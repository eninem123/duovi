import asyncio
import json
import logging
import re
import yaml
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.session import ClientSession

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class MCPAgent:
    def __init__(self, config_path="config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        
        self.stock_params = StdioServerParameters(
            command=self.config["mcp"]["stock_server"]["command"],
            args=self.config["mcp"]["stock_server"]["args"]
        )
        
        self.notebooklm_params = StdioServerParameters(
            command=self.config["mcp"]["notebooklm_server"]["command"],
            args=self.config["mcp"]["notebooklm_server"]["args"]
        )
        
        self.notebook_id = self.config["mcp"]["notebook_id"]
        self.system_prompt = self.config["agent"]["system_prompt"]

    async def _call_tool(self, server_params, tool_name, arguments):
        """通用 MCP Tool 调用方法"""
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments=arguments)
                    # result.content 是一个列表，提取文本
                    if result.content and len(result.content) > 0:
                        return result.content[0].text
                    return None
        except Exception as e:
            logging.error(f"Error calling {tool_name}: {str(e)}")
            return None

    async def get_market_data(self):
        """获取当前市场行情摘要作为决策输入"""
        logging.info("获取 A 股最新宏观与行业板块行情数据...")
        
        # 1. 获取概念板块与行业板块数据（寻找当前资金主攻方向）
        industry_data = await self._call_tool(
            self.stock_params,
            "get_industry_list",
            {}
        )
        
        # 2. 获取核心宽基指数与避险标的
        index_queries = ["上证指数", "中证500", "沪深300", "黄金ETF", "北大荒", "农业ETF"]
        index_data = await self._call_tool(
            self.stock_params, 
            "get_quotes_by_query", 
            {"queries": index_queries}
        )
        
        # 组合成完整的市场上下文
        market_context = f"""
        【宽基指数与核心标的实时状态】：
        {index_data}
        
        【当前行业板块资金流向与涨跌概况】：
        {industry_data}
        """
        return market_context

    async def update_holdings_prices(self, symbols):
        """批量获取持仓股票最新价格"""
        if not symbols:
            return {}
            
        result_text = await self._call_tool(
            self.stock_params, 
            "get_quotes_by_query", 
            {"queries": symbols}
        )
        
        price_dict = {}
        if result_text:
            try:
                # 假设 stock-sdk-mcp 返回的是 JSON 字符串
                data = json.loads(result_text)
                for item in data:
                    symbol = item.get("symbol") or item.get("code")
                    price = item.get("price") or item.get("current")
                    if symbol and price:
                        price_dict[symbol] = float(price)
            except Exception as e:
                logging.error(f"解析行情数据失败: {str(e)}")
                
        return price_dict

    async def make_decision(self, market_data):
        """调用 NotebookLM 进行多域预判决策（带 Mock 容错）"""
        logging.info("调用 NotebookLM 智能体进行多域预判...")
        
        prompt = f"""
        {self.system_prompt}
        
        【当前A股行情摘要】：
        {market_data}
        """
        
        result_text = await self._call_tool(
            self.notebooklm_params,
            "ask_question",
            {
                "notebook_id": self.notebook_id,
                "question": prompt
            }
        )
        
        if not result_text:
            logging.warning("NotebookLM 未返回结果或调用失败，触发战时容错机制：不采取任何行动，强制保持空仓。")
            return None, prompt, None
            
        # 尝试提取 JSON
        try:
            # 找到大括号之间的内容
            json_str = re.search(r'\{.*\}', result_text, re.DOTALL)
            if json_str:
                decision = json.loads(json_str.group())
                return decision, prompt, result_text
            else:
                logging.error("无法从 NotebookLM 返回结果中提取 JSON。")
                return None, prompt, result_text
        except json.JSONDecodeError as e:
            logging.error(f"解析决策 JSON 失败: {str(e)}")
            return None, prompt, result_text

# 简单的测试运行块
if __name__ == "__main__":
    async def test():
        agent = MCPAgent()
        market_data = await agent.get_market_data()
        print("Market Data:", market_data)
        
        # decision, _, _ = await agent.make_decision(market_data)
        # print("Decision:", decision)
        
    asyncio.run(test())
