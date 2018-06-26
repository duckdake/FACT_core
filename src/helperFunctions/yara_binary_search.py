from os.path import basename
from subprocess import check_output, CalledProcessError, STDOUT
from tempfile import NamedTemporaryFile

import yara
from typing import Tuple, List, Optional

from helperFunctions.web_interface import ConnectTo
from storage.db_interface_common import MongoInterfaceCommon
from storage.fs_organizer import FS_Organizer


class YaraRuleError(Exception):
    pass


class YaraBinarySearchScanner:

    def __init__(self, config=None):
        self.matches = []
        self.config = config
        self.db_path = self.config['data_storage']['firmware_file_storage_directory']

    def _execute_yara_search(self, rule_file_path, target_path=None):
        '''
        scans the (whole) db directory with the provided rule file and returns the (raw) results
        yara-python cannot be used, because it (currently) supports single-file scanning only
        :param rule_file_path: file path to yara rule file
        :return: output from yara scan
        '''
        try:
            scan_result = check_output('yara -r {} {}'.format(rule_file_path, self.db_path if target_path is None else target_path), shell=True, stderr=STDOUT)
        except CalledProcessError as e:
            raise YaraRuleError('There seems to be an error in the rule file:\n{}'.format(e.output.decode()))
        return scan_result

    def _execute_yara_search_for_single_firmware(self, rule_file_path, firmware_uid):
        with ConnectTo(YaraBinarySearchScannerDbInterface, self.config) as connection:
            file_paths = connection.get_file_paths_of_files_included_in_fo(firmware_uid)
        result = (self._execute_yara_search(rule_file_path, path) for path in file_paths)
        return b'\n'.join(result)

    @staticmethod
    def _parse_raw_result(raw_result):
        '''
        :param raw_result: raw yara scan result
        :return: dict of matching rules with lists of matched UIDs as values
        '''
        results = {}
        for line in raw_result.split(b'\n'):
            if line and b'warning' not in line:
                rule, match = line.decode().split(' ')
                match = basename(match)
                if rule in results:
                    results[rule].append(match)
                else:
                    results[rule] = [match]
        return results

    @staticmethod
    def _eliminate_duplicates(result_dict):
        for key in result_dict:
            result_dict[key] = sorted(set(result_dict[key]))

    def get_binary_search_result(self, task: Tuple[bytes, Optional[str]]):
        '''
        :param task: tuple containing the yara_rules (byte string with the contents of the yara rule file) and optionally a firmware uid if only the contents
                     of a single firmware are to be scanned
        :return: dict of matching rules with lists of (unique) matched UIDs as values
        '''
        with NamedTemporaryFile() as temp_rule_file:
            yara_rules, firmware_uid = task
            try:
                compiled_rules = yara.compile(source=yara_rules.decode())
            except yara.SyntaxError as e:
                return YaraRuleError('There seems to be an error in the rule file:\n{}'.format(e))
            compiled_rules.save(file=temp_rule_file)
            temp_rule_file.flush()

            try:
                if firmware_uid is None:
                    raw_result = self._execute_yara_search(temp_rule_file.name)
                else:
                    raw_result = self._execute_yara_search_for_single_firmware(temp_rule_file.name, firmware_uid)
            except YaraRuleError as e:
                return e
            results = self._parse_raw_result(raw_result)
            if results:
                self._eliminate_duplicates(results)
            return results


def is_valid_yara_rule_file(rules_file):
    return get_yara_error(rules_file) is None


def get_yara_error(rules_file):
    if type(rules_file) == bytes:
        rules_file = rules_file.decode()
    try:
        yara.compile(source=rules_file)
        return None
    except Exception as e:
        return e


class YaraBinarySearchScannerDbInterface(MongoInterfaceCommon):

    READ_ONLY = True

    def get_file_paths_of_files_included_in_fo(self, fo_uid: str) -> List[str]:
        fs_organizer = FS_Organizer(self.config)
        return [
            fs_organizer.generate_path_from_uid(uid)
            for uid in self.get_uids_of_all_included_files(fo_uid)
        ]
