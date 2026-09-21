"""Every archive product in the project, in load order.

The PostGIS layer consults only this list to learn which tables to create, which partitions to
load, and in what order: sites first, then observations, then each downstream component's
products. A component adds its products here and nowhere else.
"""

from aqdt.calibration.products import CALIBRATED_HOURLY, FITS, SENSOR_HOURLY
from aqdt.observation_store.products import OBSERVATIONS, SITES, Product

# @spec OBS-PG-012, CAL-STORE-007
PRODUCTS: list[Product] = [SITES, OBSERVATIONS, SENSOR_HOURLY, FITS, CALIBRATED_HOURLY]
