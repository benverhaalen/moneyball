"""Installed MCP protocol exposes the complete connected-agent surface."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

from art_of_the_deal.service import Service
from test_service import league, forecasts


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Install the agent extra for protocol checks')
class OnboardingMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_onboarding_selection_lineup_and_pipeline_through_stdio(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as root:
            s = Service(root)
            data, _ = league(position_limits={})
            data['own_team_id'] = None
            s.store.put('config','friends',{'platform':'fixture','league_id':'L','season':2026,'own_team_id':None})
            s._publish('friends',data,[],[])
            s.store.put('forecasts','friends:test',forecasts(data,{'aq':10,'ar':8,'bq':9,'br':11}))
            params = StdioServerParameters(command=sys.executable,args=['-m','art_of_the_deal.server'],env={**os.environ,'ART_OF_DEAL_HOME':root,'MONEYBALL_LAB_DIR':os.path.join(root,'lab')})
            with tempfile.TemporaryFile(mode='w+') as log:
                async with stdio_client(params,errlog=log) as (read,write):
                    async with ClientSession(read,write) as client:
                        await client.initialize()
                        tools = {t.name for t in (await client.list_tools()).tools}
                        self.assertTrue({'discover_sleeper_leagues','onboarding_status','select_team','draft_context','lineup_packet','pipeline_catalog','sync_pipeline','query_pipeline'} <= tools)
                        status = await client.call_tool('onboarding_status',{'alias':'friends'})
                        self.assertFalse(status.isError)
                        self.assertFalse(json.loads(status.content[0].text)['leagues'][0]['team_selected'])
                        selected = await client.call_tool('select_team',{'alias':'friends','team_id':'a'})
                        self.assertFalse(selected.isError)
                        self.assertEqual(json.loads(selected.content[0].text)['own_team_id'],'a')
                        lineup = await client.call_tool('lineup_packet',{'alias':'friends','fresh':False})
                        self.assertFalse(lineup.isError)
                        self.assertEqual(json.loads(lineup.content[0].text)['strategy']['research']['status'],'pending_agent_research')
                        catalog = await client.call_tool('pipeline_catalog',{})
                        self.assertFalse(catalog.isError)


if __name__ == '__main__': unittest.main()
