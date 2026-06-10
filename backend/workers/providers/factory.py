from backend.workers.providers.base import BaseWeatherProvider
from backend.workers.providers.ecmwf import ECMWFProvider
from backend.workers.providers.imd import IMDProvider

class WeatherProviderFactory:
    """
    Factory class that returns the active weather provider instance.
    """

    @staticmethod
    def get_provider(provider_name: str) -> BaseWeatherProvider:
        """
        Instantiates and returns the requested weather provider class.
        Defaults to ECMWF if the provider name is unrecognized.
        """
        name = (provider_name or "ecmwf").strip().lower()
        if name == "imd":
            return IMDProvider()
        return ECMWFProvider()
