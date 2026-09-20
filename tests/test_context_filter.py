import copy
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda


class ContextFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_remove_join_keys_from_selected_table(self):
        module = importlib.import_module("app.agent.nodes.filter_table")
        state = {
            "query": "销售额",
            "table_infos": [
                {
                    "name": "fact_order",
                    "role": "fact",
                    "description": "订单",
                    "columns": [
                        {"name": "order_id", "role": "primary_key"},
                        {"name": "region_id", "role": "foreign_key"},
                        {"name": "order_amount", "role": "measure"},
                        {"name": "unused", "role": "dimension"},
                    ],
                }
            ],
        }
        original = copy.deepcopy(state)
        runtime = SimpleNamespace(stream_writer=lambda event: None)
        with patch.object(
            module,
            "llm",
            RunnableLambda(lambda prompt: '{"fact_order":["order_amount"]}'),
        ):
            result = await module.filter_table(state, runtime)
        names = {column["name"] for column in result["table_infos"][0]["columns"]}
        self.assertEqual(names, {"order_id", "region_id", "order_amount"})
        self.assertEqual(state, original)
