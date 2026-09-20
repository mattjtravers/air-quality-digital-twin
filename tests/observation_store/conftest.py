"""Shared record builders for observation-store tests.

Every builder returns a valid record; tests override the fields they exercise.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pandera.pandas as pa
import pytest

from aqdt.observation_store.products import Product
from aqdt.observation_store.schemas import Observation, Site, SiteType, Source

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def make_site(**overrides: Any) -> Site:
    data: dict[str, Any] = {
        "site_id": "purpleair:12345",
        "source": Source.purpleair,
        "source_native_id": "12345",
        "site_type": SiteType.low_cost_sensor,
        "name": "Test sensor",
        "latitude": 38.9072,
        "longitude": -77.0369,
    }
    data.update(overrides)
    return Site(**data)


def make_observation(**overrides: Any) -> Observation:
    data: dict[str, Any] = {
        "site_id": "purpleair:12345",
        "source": Source.purpleair,
        "observed_at": T0,
        "latitude": 38.9072,
        "longitude": -77.0369,
        "pm25_raw": 8.1,
        "pm25_channel_a": 8.0,
        "pm25_channel_b": 8.2,
        "humidity": 55.0,
        "pm25_corrected": 5.2,
        "qc_flags": [],
        "raw": {"sensor_index": 12345, "pm2.5_cf_1": 8.1},
    }
    data.update(overrides)
    return Observation(**data)


@pytest.fixture(scope="session")
def _moto_server():
    """An in-process S3 endpoint; the store sees it only through AWS_ENDPOINT_URL."""
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server._server.socket.getsockname()[1]}"
    server.stop()


@pytest.fixture
def s3_archive_uri(_moto_server, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", _moto_server)
    import boto3

    bucket = f"aqdt-test-{uuid.uuid4().hex[:8]}"
    boto3.client("s3").create_bucket(Bucket=bucket)
    return f"s3://{bucket}/archive"


# A geometry-less product, used to exercise the partition primitives and the registry
# independently of the observation schema.


class ToyFrame(pa.DataFrameModel):
    site_id: str
    hour: pa.typing.Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs={"unit": "ns", "tz": "UTC"})
    value: float

    class Config:
        strict = True
        coerce = True


TOY = Product(
    prefix="toy/values",
    partition_keys={"date": lambda f: f["hour"].dt.date},
    key_cols=["site_id", "hour"],
    frame_model=ToyFrame,
    filename="values.parquet",
    table="toy_values",
    sql_dir=None,
)


def toy_frame(rows):
    return pd.DataFrame(
        {
            "site_id": [r[0] for r in rows],
            "hour": pd.to_datetime([r[1] for r in rows], utc=True),
            "value": [float(r[2]) for r in rows],
        }
    )
