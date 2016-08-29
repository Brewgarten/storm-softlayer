import logging
import os

import pytest

from libcloud.compute.providers import get_driver


log = logging.getLogger(__name__)
logging.basicConfig(format='%(asctime)s [%(levelname)s] [%(name)s(%(filename)s:%(lineno)d)] - %(message)s', level=logging.INFO)

def getSoftLayerDriver():
    import storm.drivers.softlayer
    cls = get_driver("sl")
    return cls.ex_from_config()

@pytest.fixture(scope="module")
def softlayerDriver():
    """
    SoftLayer Cloud driver
    """
    import storm.drivers.softlayer
    if not os.path.exists(os.path.expanduser("~/.softlayer")):
        pytest.skip("requires ~/.softlayer file with account information")
    return getSoftLayerDriver()

def pytest_generate_tests(metafunc):
    if "driver" in metafunc.fixturenames:
        softlayerDriverInstance = getSoftLayerDriver()
        metafunc.parametrize("driver", [
                                pytest.mark.skipif(not os.path.exists(os.path.expanduser("~/.softlayer")),
                                                   reason="requires ~/.softlayer file with account information")
                                    (softlayerDriverInstance)
                            ])
