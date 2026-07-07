import importlib.util
from pathlib import Path
import unittest

import pandas as pd


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_target_control_eda.py"


def load_module():
    spec = importlib.util.spec_from_file_location("generate_target_control_eda", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HistogramPercentTests(unittest.TestCase):
    def test_histogram_uses_percent_axis_and_values(self):
        module = load_module()
        record = {
            "name": "测试数据",
            "df": pd.DataFrame({"目标": [0, 0, 1, 1]}),
            "field_map": {"目标": "目标"},
            "unit_map": {"目标": "%"},
        }

        fig, _ = module.histogram_kde_page_figure([record], ["目标"])

        self.assertIn("百分比", fig.layout.yaxis.title.text)
        bar_traces = [trace for trace in fig.data if trace.type == "bar"]
        self.assertTrue(bar_traces, "应该生成直方图柱状图")
        self.assertEqual(round(sum(float(value) for value in bar_traces[0].y), 6), 100.0)
        self.assertIn("百分比", bar_traces[0].hovertemplate)
        self.assertNotIn("频数", bar_traces[0].hovertemplate)


if __name__ == "__main__":
    unittest.main()
