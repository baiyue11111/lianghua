from datetime import date
import unittest

from security_identity import CodeChange, SecurityIdentity


class SecurityIdentityTests(unittest.TestCase):
    def test_aliases_share_identity_but_trading_code_is_date_dependent(self):
        registry = SecurityIdentity((
            CodeChange('SZ', '000010', '001010', date(2020, 1, 2), 'announcement'),
            CodeChange('SZ', '001010', '002010', date(2022, 1, 3), 'announcement'),
        ))
        self.assertEqual(registry.identity('SZ', '002010'), ('SZ', '000010'))
        self.assertEqual(registry.code_at('SZ', '002010', date(2019, 12, 31)), '000010')
        self.assertEqual(registry.code_at('SZ', '000010', date(2020, 1, 2)), '001010')
        self.assertEqual(registry.code_at('SZ', '001010', date(2022, 1, 3)), '002010')

    def test_unknown_alias_is_not_silently_assumed_verified(self):
        with self.assertRaises(KeyError):
            SecurityIdentity(()).identity('SZ', '000001')

    def test_ambiguous_or_cyclic_or_nonchronological_history_is_rejected(self):
        a = CodeChange('SZ', '000010', '001010', date(2020, 1, 2), 'source')
        for b in (
            CodeChange('SZ', '000010', '002010', date(2021, 1, 2), 'source'),
            CodeChange('SZ', '002010', '001010', date(2021, 1, 2), 'source'),
            CodeChange('SZ', '001010', '000010', date(2021, 1, 2), 'source'),
            CodeChange('SZ', '001010', '002010', date(2019, 1, 2), 'source'),
        ):
            with self.subTest(change=b), self.assertRaises(ValueError):
                SecurityIdentity((a, b))

    def test_missing_source_and_self_transition_rejected(self):
        for old, new, source in [('000010', '001010', ''), ('000010', '000010', 'source')]:
            with self.assertRaises(ValueError):
                SecurityIdentity((CodeChange('SZ', old, new, date(2020, 1, 2), source),))
