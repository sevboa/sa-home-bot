from sa_home_bot.sensors.gpu import parse_nvidia_smi_csv

from .conftest import BASE_TIME


def test_parse_nvidia_smi_csv_multiple_cards():
    # Формат `nvidia-smi --query-gpu=index,name,temperature.gpu --format=csv,noheader,nounits`.
    output = "0, Tesla V100-SXM2-16GB, 41\n1, NVIDIA GeForce RTX 3060, 38\n"
    readings = parse_nvidia_smi_csv(output, BASE_TIME)
    assert len(readings) == 2
    v100, rtx = readings
    assert v100.component_id == "gpu:0"
    assert v100.kind == "gpu"
    assert v100.label == "Tesla V100-SXM2-16GB"
    assert v100.temperature_c == 41.0
    assert rtx.component_id == "gpu:1"
    assert rtx.label == "NVIDIA GeForce RTX 3060"
    assert rtx.temperature_c == 38.0


def test_parse_nvidia_smi_csv_skips_na_temperature():
    # Карта временно не отдаёт температуру ([N/A]) — не падаем, пропускаем строку.
    output = "0, Tesla V100-SXM2-16GB, [N/A]\n"
    assert parse_nvidia_smi_csv(output, BASE_TIME) == []


def test_parse_nvidia_smi_csv_empty_output():
    assert parse_nvidia_smi_csv("", BASE_TIME) == []
