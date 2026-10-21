"""Opt-in trusted setup; imports existing handlers, never generated packages."""
def install_existing(service):
    from core.home.main_adapter import registry_from_environment
    from core.home.smartthings_pc import controller_from_environment
    from rio_dubbing_client import DubbingClient,tool_definitions
    home=registry_from_environment()
    try:
        pc=controller_from_environment();dubbing=DubbingClient()
        service.adapters.home(home);service.adapters.pc(pc)
        service.adapters.dubbing(dubbing,tool_definitions())
    except BaseException:
        home.close();raise
    return [home]
