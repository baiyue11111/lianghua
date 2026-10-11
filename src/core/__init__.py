"""Market-neutral core, with no eager imports of application or outer layers.

contracts: immutable per-step domain data.
backtest: public use-case request, response and error data.
interfaces: plug-in ports and component assembly port.
kernel: fixed orchestration and isolated run lifecycle.

Import each dependency from its owning module explicitly.
"""
