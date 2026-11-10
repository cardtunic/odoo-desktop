import unittest

from configure_br_tax_labels import formatted_number


class TaxFormattingTests(unittest.TestCase):
    def test_cpf_raw_formatted_and_leading_zero(self):
        self.assertEqual(formatted_number('52998224725'), '529.982.247-25')
        self.assertEqual(formatted_number('529.982.247-25'), '529.982.247-25')
        self.assertEqual(formatted_number('01234567890'), '012.345.678-90')

    def test_cnpj_raw_and_formatted(self):
        self.assertEqual(formatted_number('11222333000181'), '11.222.333/0001-81')
        self.assertEqual(formatted_number('11.222.333/0001-81'), '11.222.333/0001-81')

    def test_never_pads_or_truncates_invalid_values(self):
        for value in ['1234567890', '123456789012', '123456789012345', 'foreign-tax-id', None, '']:
            self.assertEqual(formatted_number(value), value or '')


if __name__ == '__main__':
    unittest.main()
