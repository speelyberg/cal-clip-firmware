import unittest
from pathlib import Path

from warning_gate import check, warnings

WS, HOME = '/home/runner/work/r/r', '/home/runner'
BASELINE = {l for l in (Path(__file__).with_name('warning-baseline.txt')).read_text().splitlines()
            if l and not l.startswith('#')}
OURS = ['respeaker_clip/applications/clip/', 'respeaker_clip/drivers/new.c']
SPEEX = f'{WS}/respeaker_clip/lib/speexdsp/speexdsp/libspeexdsp/pseudofloat.h:63: warning: "MIN" redefined'


def gate(log):
    return check(warnings(log, WS, HOME), BASELINE, OURS)


class WarningGateTests(unittest.TestCase):
    def test_stock_warnings_pass_raw_or_from_gh_log(self):
        self.assertEqual(gate(SPEEX + '\nwarning: Deprecated symbol MBEDTLS_MD is enabled.\n'), ([], []))
        self.assertEqual(gate(f'debug application\tstep\t2026-10-24T19:06:20.9566478Z {SPEEX}\n'), ([], []))

    def test_new_warning_in_app_fails_both_rules(self):
        mine, new = gate(f'{WS}/respeaker_clip/applications/clip/src/button.c:12:5: warning: unused variable "x"\n')
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(new), 1)

    def test_owned_file_fails_even_when_text_is_baselined(self):
        mine, _ = gate(f'{WS}/respeaker_clip/drivers/new.c:63: warning: "MIN" redefined\n')
        self.assertEqual(mine, ['respeaker_clip/drivers/new.c:63: warning: "MIN" redefined'])

    def test_new_third_party_or_kconfig_warning_fails(self):
        self.assertEqual(len(gate(SPEEX.replace(':63:', ':64:'))[1]), 1)
        self.assertEqual(len(gate('warning: Deprecated symbol FOO is enabled.')[1]), 1)
        self.assertEqual(gate('note: warnings are fine to mention'), ([], []))


if __name__ == '__main__':
    unittest.main()
