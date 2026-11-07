"""Unit tests for the RCAFlow Knowledge Base K = (Kp, Kr, Kt)."""
import unittest
from agent.knowledge import (
    KnowledgeBase,
    get_knowledge_base,
    retrieve,
    retrieve_full,
    get_tools_knowledge,
    get_global_rules,
)
from agent.workflows import retrieve as workflow_retrieve, WORKFLOWS


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.kb = get_knowledge_base()

    def test_templates_loaded_and_valid(self):
        expected_templates = {
            "multimodal_rca",
            "cpu_exhaustion",
            "memory_jvm",
            "disk_io",
            "network_delay",
            "database_lifecycle",
        }
        self.assertTrue(expected_templates.issubset(set(self.kb.templates.keys())))
        for name, template in self.kb.templates.items():
            self.assertTrue(len(template.steps) > 0)
            self.assertTrue(template.control in {"sequence", "selector", "parallel"})
            self.assertTrue(len(template.tags) > 0)
            self.assertTrue(isinstance(template.rules, list))

    def test_tools_knowledge_kt(self):
        kt = get_tools_knowledge()
        for tool in ["profile", "metrics", "logs", "traces"]:
            self.assertIn(tool, kt)
            self.assertIn("args", kt[tool])
            self.assertIn("returns", kt[tool])
            self.assertIn("preconditions", kt[tool])

    def test_global_rules_kr(self):
        kr = get_global_rules()
        self.assertTrue(len(kr) >= 5)
        self.assertTrue(any("baseline" in r.lower() for r in kr))
        self.assertTrue(any("utc+8" in r.lower() for r in kr))

    def test_query_retrieval_routing(self):
        # CPU queries
        res = self.kb.retrieve("The server shows extreme cpu spike and high processor utilization")
        top_name = res["kp"][0]["name"]
        self.assertEqual(top_name, "cpu_exhaustion")
        self.assertIn("metrics", res["kt"])

        # Memory / JVM queries
        res = self.kb.retrieve("Tomcat container crashed with JVM Out of Memory Heap and high GC pause")
        top_name = res["kp"][0]["name"]
        self.assertEqual(top_name, "memory_jvm")
        self.assertIn("logs", res["kt"])

        # Network queries
        res = self.kb.retrieve("Client observed high network latency and packet loss between services")
        top_name = res["kp"][0]["name"]
        self.assertEqual(top_name, "network_delay")
        self.assertIn("traces", res["kt"])

        # Disk queries
        res = self.kb.retrieve("Database node experiences severe disk read I/O load and filesystem exhaustion")
        top_name = res["kp"][0]["name"]
        self.assertEqual(top_name, "disk_io")

        # Database connection queries
        res = self.kb.retrieve("Service failed due to db connection limit reached and container process termination")
        top_name = res["kp"][0]["name"]
        self.assertEqual(top_name, "database_lifecycle")

    def test_fallback_mechanism_below_threshold(self):
        # Irrelevant nonsense query with no matching keywords
        res = self.kb.retrieve("xylophone quantum sandwich", top_k=2)
        self.assertTrue(res["fallback_applied"])
        self.assertEqual(res["kp"][0]["name"], "multimodal_rca")

    def test_workflow_backward_compatibility(self):
        results = workflow_retrieve("Check high cpu load on apache01")
        self.assertTrue(len(results) > 0)
        top = results[0]
        self.assertIn("name", top)
        self.assertIn("control", top)
        self.assertIn("steps", top)
        self.assertIn("rules", top)
        self.assertIn("structured_steps", top)
        self.assertEqual(top["name"], "cpu_exhaustion")
        self.assertTrue(len(WORKFLOWS) >= 3)


if __name__ == "__main__":
    unittest.main()

