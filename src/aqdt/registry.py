"""Every archive product in the project, in load order.

The PostGIS layer consults only this list to learn which tables to create, which partitions to
load, and in what order: sites first, then observations, then each downstream component's
products. A component adds its products here and nowhere else.
"""

from aqdt.observation_store.products import OBSERVATIONS, SITES, Product

# @spec OBS-PG-012
PRODUCTS: list[Product] = [SITES, OBSERVATIONS]
