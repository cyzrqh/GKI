import unittest
from unittest.mock import patch

from verify_requested_features import requests_from_environment, verify_requested


class RequestedFeatureTests(unittest.TestCase):
    def test_requested_kpm_susfs_missing_from_actual_config_cannot_publish(self):
        for name in ('use_kpm', 'enable_susfs'):
            with self.assertRaisesRegex(ValueError, name):
                verify_requested({'CONFIG_ARM64': 'y'}, {name: True})

    def test_only_requested_features_are_required_and_both_lz4kd_abis_work(self):
        verify_requested({}, {'use_kpm': False})
        for key in ('CONFIG_ZRAM_BACKEND_LZ4KD', 'CONFIG_CRYPTO_LZ4KD'):
            verify_requested({key: 'y'}, {'use_lz4kd': True})

    def test_networking_needs_both_real_features(self):
        with self.assertRaises(ValueError):
            verify_requested({'CONFIG_TCP_CONG_BBR': 'y'}, {'use_networking': True})
        verify_requested({'CONFIG_TCP_CONG_BBR': 'y', 'CONFIG_IP_SET': 'y'}, {'use_networking': True})

    def test_flag_transport_does_not_read_or_expose_signing_secrets(self):
        with patch.dict('os.environ', {'GKI_REQUEST_USE_KPM': 'true', 'SUKISU_APK_PASSWORD': 'private'}, clear=True):
            self.assertEqual({'use_kpm': True}, requests_from_environment())


if __name__ == '__main__':
    unittest.main()
