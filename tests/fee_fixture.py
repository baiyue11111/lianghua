"""Explicit engineering policy; never a production default."""
from core.calculation import FeePolicy, TaxPolicy, TradeRules

def fees():
    return FeePolicy.from_mapping({'commission_rate':'.00025','minimum_commission':'5','rounding':'none',
        'stamp_rates':[{'effective':'2015-09-09','rate':'.001'},{'effective':'2023-08-28','rate':'.0005'}],
        'transfer_rates':[{'effective':'2015-09-09','rate':'0'}]},'a'*64)

def tax_policy():
    return TaxPolicy.from_mapping({'effective':'2015-09-09','policy':'财税2015-101',
        'payment_timing':'later_of_disposal_and_income','liability':'current_holding_period','rounding':'none',
        'bonus_holding_period':'originating_lot','bonus_tax_allocation':'proportional_entitlement'},'b'*64)

def rules():
    return TradeRules(execution(), fees(), tax_policy())

def execution():
    return {
        'price': {'model':'ratio','params':{'rate':'.001','tick':'.01'}},
        'quantity': {'model':'capacity','params':{}},
        'fees': {'model':'dated','params':{'rounding':'none'}},
        'tax': {'model':'fifo','params':{}},
        'check': {'model':'cn_a_share','params':{'tick':'.01','min_qty':100,'step':100}},
    }

def settlement(initial_cash):
    from core.calculation import AllocationLimits
    from plugins.data import Settlement
    return Settlement(initial_cash, fees(), tax_policy(), limits=AllocationLimits(), execution=execution())


def configuredSettlement(initial_cash, fees, tax_policy, *, limits, execution):
    from plugins.data import Settlement
    return Settlement(initial_cash, fees, tax_policy, limits=limits, execution=execution)


def configured_settlement():
    from backend import _load_plugin
    return _load_plugin({"settlement": {
        "module": "tests.fee_fixture:configuredSettlement",
        "params": {"limits": {"stock_weight": "0.4", "adv_fraction": "0.01"},
                    "execution": execution()},
    }}, "settlement")
