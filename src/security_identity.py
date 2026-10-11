"""Explicit, source-backed code histories. No I/O or price-factor inference.

Identity groups historical aliases for accounting, not point-in-time stock
selection. An absent alias is unknown, not proof of unchanged identity.
"""
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType


@dataclass(frozen=True)
class CodeChange:
    exchange: str
    old_code: str
    new_code: str
    effective_date: date
    source: str


class SecurityIdentity:
    def __init__(self, changes: tuple[CodeChange, ...]):
        outgoing, incoming = {}, {}
        for change in changes:
            if not change.exchange or not change.source.strip():
                raise ValueError('exchange and announcement source required')
            if not isinstance(change.effective_date, date):
                raise ValueError('effective date required')
            if any(len(code) != 6 or not code.isdigit()
                   for code in (change.old_code, change.new_code)):
                raise ValueError('invalid security code')
            old = (change.exchange, change.old_code)
            new = (change.exchange, change.new_code)
            if old == new or old in outgoing or new in incoming:
                raise ValueError('ambiguous code history')
            outgoing[old] = change
            incoming[new] = change
        histories = {}
        for alias in outgoing.keys() | incoming.keys():
            root, visited = alias, set()
            while root in incoming:
                if root in visited:
                    raise ValueError('cyclic code history')
                visited.add(root)
                root = (root[0], incoming[root].old_code)
            chain, current = [], root
            while current in outgoing:
                change = outgoing[current]
                if chain and change.effective_date <= chain[-1].effective_date:
                    raise ValueError('nonchronological code history')
                chain.append(change)
                current = (current[0], change.new_code)
            histories[alias] = (root, tuple(chain))
        self._histories = MappingProxyType(histories)

    def identity(self, exchange: str, code: str) -> tuple[str, str]:
        return self._histories[(exchange, code)][0]

    def code_at(self, exchange: str, code: str, as_of: date) -> str:
        root, chain = self._histories[(exchange, code)]
        result = root[1]
        for change in chain:
            if change.effective_date > as_of:
                break
            result = change.new_code
        return result
