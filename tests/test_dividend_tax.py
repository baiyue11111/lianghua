from datetime import date
from decimal import Decimal
import unittest

from dividend_tax import dividend_tax, TaxLot, allocate_fifo


class DividendTaxTests(unittest.TestCase):
    def test_official_2013_withholding_and_supplement_examples(self):
        # Tax authority example: May24 purchase, Jun18 record, gross4000.
        for sold, final, supplement in [('2013-06-24','800','600'),('2013-06-25','400','200'),('2014-05-24','400','200'),('2014-05-25','200','0')]:
            with self.subTest(sold=sold):
                result=dividend_tax(date(2013,5,24),date.fromisoformat(sold),date(2013,6,18),Decimal('4000'))
                self.assertEqual(result.withheld,Decimal('200'))
                self.assertEqual(result.final_tax,Decimal(final))
                self.assertEqual(result.supplement,Decimal(supplement))

    def test_natural_month_end_not_thirty_days(self):
        for acquired in (date(2013,1,28),date(2013,1,31)):
            short=dividend_tax(acquired,date(2013,2,28),date(2013,2,1),Decimal('100'))
            longer=dividend_tax(acquired,date(2013,3,1),date(2013,2,1),Decimal('100'))
            self.assertEqual(short.final_tax,Decimal('20'))
            self.assertEqual(longer.final_tax,Decimal('10'))

    def test_post_2015_no_prewithholding_and_long_term_exemption(self):
        for sold,tax in [('2021-07-01','20'),('2021-07-02','10'),('2022-06-01','10'),('2022-06-02','0')]:
            result=dividend_tax(date(2021,6,1),date.fromisoformat(sold),date(2021,6,15),Decimal('100'))
            self.assertEqual(result.withheld,0)
            self.assertEqual(result.final_tax,Decimal(tax))

    def test_invalid_dates_and_money_fail_before_calculation(self):
        for income in (Decimal('-1'),Decimal('NaN'),Decimal('Infinity')):
            with self.assertRaises(ValueError):
                dividend_tax(date(2020,1,1),date(2020,2,1),date(2020,1,10),income)
        with self.assertRaises(ValueError):
            dividend_tax(date(2020,1,1),date(2020,1,2),date(2020,1,10),Decimal('1'))

    def test_fifo_matches_official_13000_share_example(self):
        lots=(TaxLot('a',date(2012,5,15),8000),TaxLot('b',date(2013,4,3),2000),TaxLot('c',date(2013,6,6),5000))
        allocated,remaining=allocate_fifo(lots,13000,date(2013,6,11))
        self.assertEqual([(lot.lot_id,lot.shares) for lot in allocated],[('a',8000),('b',2000),('c',3000)])
        self.assertEqual(remaining,(TaxLot('c',date(2013,6,6),2000),))
        self.assertEqual(lots[2].shares,5000)
        with self.assertRaises(ValueError): allocate_fifo(lots,16000,date(2013,6,11))
        with self.assertRaises(ValueError): allocate_fifo(lots,-1,date(2013,6,11))


if __name__=='__main__': unittest.main()
