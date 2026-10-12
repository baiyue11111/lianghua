"""Explicit engineering policy; never a production default."""
from fees import FeePolicy
from tax_ledger import TaxPolicy

def fees():
    return FeePolicy.from_mapping({'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
        'stamp_rates':[{'effective':'2015-09-09','rate':'.001'},{'effective':'2023-08-28','rate':'.0005'}],
        'transfer_rates':[{'effective':'2015-09-09','rate':'0'}]},'a'*64)

def tax_policy():
    return TaxPolicy.from_mapping({'effective':'2015-09-09','policy':'财税2015-101',
        'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period','rounding':'none',
        'bonus_holding_period':'originating_lot','bonus_tax_allocation':'proportional_entitlement'},'b'*64)
