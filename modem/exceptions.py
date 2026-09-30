class ModemError(Exception):
    """Messages must be fixed text, never device responses or request data."""


class ModemConnectionError(ModemError):
    pass


class ModemAuthenticationError(ModemError):
    pass


class ModemSessionExpired(ModemAuthenticationError):
    pass


class ModemAPIError(ModemError):
    pass


class ModemRateLimited(ModemError):
    pass
