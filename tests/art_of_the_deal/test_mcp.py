"""Actual MCP stdio journey, not just direct function calls."""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

from art_of_the_deal.service import Service
from test_service import league, forecasts, proposal
from test_league_research import report

@unittest.skipUnless(importlib.util.find_spec("mcp"), "Run in the project venv for MCP integration tests")
class MCPJourney(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_trade_and_strategy_without_network_or_model(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as root:
            s=Service(root)
            d,p=league(position_limits={})
            s.store.put("config","demo",{"platform":"fixture","league_id":"L","season":2026})
            s.store.put("strategy",p["rules_fingerprint"],p)
            s.store.put("league","demo",d)
            s.store.put("forecasts","demo:test",forecasts(d,{"aq":10,"ar":8,"bq":10,"br":12,"fa3":15}))
            s.store.put("pool","demo",{"players":[{"player_id":"fa3","name":"Available RB","positions":["RB"],"acquisition_status":"WAIVERS","execution_actionable":None}],
                                      "observed_at":time.time(),"source":{"url":"https://example.test/pool"},
                                      "horizon":{"kind":"week","season":2026,"week":1},"waiver_state":{"own_current_rank":2}})
            params=StdioServerParameters(command=sys.executable,args=["-m","art_of_the_deal.server"],env={**os.environ,"ART_OF_DEAL_HOME":root})
            with tempfile.TemporaryFile(mode="w+") as log:
                async with stdio_client(params,errlog=log) as (read,write):
                    async with ClientSession(read,write) as client:
                        await client.initialize()
                        names={t.name for t in (await client.list_tools()).tools}
                        self.assertIn("trade_packet",names)
                        self.assertIn("acquisition_context",names)
                        self.assertIn("pickup_packet",names)
                        self.assertIn("league_research_plan",names)
                        self.assertIn("save_league_research",names)
                        self.assertNotIn("submit_trade",names)
                        strategy=await client.call_tool("league_strategy",{"alias":"demo"})
                        self.assertFalse(strategy.isError)
                        research_plan = await client.call_tool("league_research_plan", {"alias": "demo"})
                        plan = json.loads(research_plan.content[0].text)
                        saved = await client.call_tool("save_league_research", {"alias": "demo", "report": report(plan)})
                        self.assertFalse(saved.isError)
                        skill = await client.read_resource("moneyball://skills/strategy")
                        self.assertIn("name: moneyball-strategy", skill.contents[0].text)
                        method = await client.read_resource("moneyball://research/method")
                        self.assertIn("Bühlmann", method.contents[0].text)
                        result=await client.call_tool("trade_packet",{"alias":"demo","proposal":proposal(),"fresh":False})
                        self.assertFalse(result.isError)
                        data=json.loads(result.content[0].text)
                        self.assertIsNone(data["verdict"])
                        conditional = data["decision_support"]["answer_contract"]["conditional_recommendation"]
                        self.assertIn("if/otherwise", conditional)
                        self.assertIn("actual substitutions and costs", conditional)
                        self.assertIn("only when coherent inputs", conditional)
                        self.assertEqual(data["strategy"]["research"]["status"], "saved_agent_research")
                        self.assertIsNone(data["championship_probability_delta"])
                        self.assertEqual(data["mechanics"]["comparisons"][0]["teams"]["a"]["delta_provider_baseline_points"],4)
                        self.assertEqual(data["strategy"]["horizon"]["residual_seasons"],0)
                        before=s.store.get("league","demo")["sha256"]
                        bad=await client.call_tool("trade_packet",{"alias":"demo","proposal":{"transfers":[{"player_id":"br","from_team":"a","to_team":"b"}]},"fresh":False})
                        self.assertFalse(json.loads(bad.content[0].text)["mechanics"]["valid"])
                        self.assertEqual(s.store.get("league","demo")["sha256"],before)
                        pickup=await client.call_tool("pickup_packet",{"alias":"demo","player_id":"fa3","drop_player_id":"ar","evaluation_week":1,"fresh":False})
                        self.assertFalse(pickup.isError)
                        pickup_data=json.loads(pickup.content[0].text)
                        self.assertEqual(pickup_data["comparison"]["comparisons"][0]["delta_points"],7)
                        claims=await client.call_tool("claim_scenarios",{"alias":"demo","claims":[{"player_id":"fa3","drop_player_id":"ar","priority_before":2}],"fresh":False})
                        self.assertFalse(claims.isError)
                        claim_data=json.loads(claims.content[0].text)
                        self.assertIsNone(claim_data["comparison"]["claim_probabilities"])
                        self.assertEqual(s.store.get("league","demo")["sha256"],before)

if __name__=="__main__": unittest.main()
