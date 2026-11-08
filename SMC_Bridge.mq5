//+------------------------------------------------------------------+
//|                                                   SMC_Bridge.mq5 |
//|                             Copyright 2026, Forex SMC AutoTrader |
//|                                      https://github.com/smc_bot  |
//+------------------------------------------------------------------+
#property copyright "Forex SMC AutoTrader"
#property link      "https://github.com/smc_bot"
#property version   "1.00"
#property description "Automated execution bridge between Python SMC Bot and MetaTrader 5"

#include <Trade\Trade.mqh>

input group "=== Bridge Configuration ==="
input string   InpServerURL     = "http://127.0.0.1:5001/api/sync"; // Bridge Sync URL
input int      InpPollInterval  = 1;                                // Sync Interval (Seconds)
input ulong    InpMagicNumber   = 888999;                           // Magic Number
input ulong    InpSlippage      = 10;                               // Max Slippage (points)

CTrade trade;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagicNumber);
   trade.SetDeviationInPoints(InpSlippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);

   EventSetTimer(InpPollInterval);
   Print("SMC_Bridge initialized. Connecting to Python bridge at: ", InpServerURL);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("SMC_Bridge stopped.");
}

//+------------------------------------------------------------------+
//| Timer event function                                             |
//+------------------------------------------------------------------+
void OnTimer()
{
   SyncWithPython();
}

//+------------------------------------------------------------------+
//| Helper: Escape JSON String                                       |
//+------------------------------------------------------------------+
string JsonEscape(string text)
{
   StringReplace(text, "\\", "\\\\");
   StringReplace(text, "\"", "\\\"");
   return text;
}

//+------------------------------------------------------------------+
//| Sync account & open orders, receive commands from Python         |
//+------------------------------------------------------------------+
void SyncWithPython()
{
   // 1. Build Account JSON
   string accJson = StringFormat(
      "{\"login\":\"%d\",\"balance\":%.2f,\"equity\":%.2f,\"currency\":\"%s\",\"margin\":%.2f,\"margin_free\":%.2f}",
      AccountInfoInteger(ACCOUNT_LOGIN),
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoString(ACCOUNT_CURRENCY),
      AccountInfoDouble(ACCOUNT_MARGIN),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE)
   );

   // 2. Build Open Trades JSON
   string tradesJson = "[";
   int totalPositions = PositionsTotal();
   int tradeCount = 0;
   for(int i = 0; i < totalPositions; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
      {
         if(tradeCount > 0) tradesJson += ",";
         tradesJson += StringFormat(
            "{\"id\":\"%d\",\"instrument\":\"%s\",\"currentUnits\":%.2f,\"price\":%.5f,\"unrealizedPL\":%.2f}",
            ticket,
            PositionGetString(POSITION_SYMBOL),
            PositionGetDouble(POSITION_VOLUME),
            PositionGetDouble(POSITION_PRICE_OPEN),
            PositionGetDouble(POSITION_PROFIT)
         );
         tradeCount++;
      }
   }
   tradesJson += "]";

   // 3. Build Pending Orders JSON
   string ordersJson = "[";
   int totalOrders = OrdersTotal();
   int orderCount = 0;
   for(int i = 0; i < totalOrders; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket > 0)
      {
         if(orderCount > 0) ordersJson += ",";
         ordersJson += StringFormat(
            "{\"id\":\"%d\",\"instrument\":\"%s\",\"type\":\"%s\",\"units\":%.2f,\"price\":%.5f}",
            ticket,
            OrderGetString(ORDER_SYMBOL),
            EnumToString((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE)),
            OrderGetDouble(ORDER_VOLUME_INITIAL),
            OrderGetDouble(ORDER_PRICE_OPEN)
         );
         orderCount++;
      }
   }
   ordersJson += "]";

   string payload = StringFormat(
      "{\"account\":%s,\"trades\":%s,\"orders\":%s}",
      accJson, tradesJson, ordersJson
   );

   // Send WebRequest to Python Bridge
   char postData[];
   char result[];
   string resultHeaders;
   StringToCharArray(payload, postData, 0, StringLen(payload));

   string headers = "Content-Type: application/json\r\n";
   int res = WebRequest("POST", InpServerURL, headers, 3000, postData, result, resultHeaders);

   if(res == 200)
   {
      string responseText = CharArrayToString(result);
      ProcessServerResponse(responseText);
   }
   else if(res == -1)
   {
      int err = GetLastError();
      // Error 4014 means URL not added in MT5 Options -> Expert Advisors
      if(err == 4014)
      {
         Print("ERROR 4014: Please add 'http://127.0.0.1:5001' to Tools -> Options -> Expert Advisors -> Allow WebRequest");
      }
   }
}

//+------------------------------------------------------------------+
//| Simple JSON Command Parser & Executor                            |
//+------------------------------------------------------------------+
void ProcessServerResponse(string json)
{
   if(StringFind(json, "\"commands\":") < 0) return;

   // 1. Check for CLOSE_ALL command
   if(StringFind(json, "\"CLOSE_ALL\"") >= 0)
   {
      Print("Executing CLOSE_ALL command...");
      CloseAllPositionsAndOrders();
      return;
   }

   // 2. Check for ORDER_LIMIT commands
   int pos = 0;
   while((pos = StringFind(json, "{\"action\":\"ORDER_LIMIT\"", pos)) >= 0)
   {
      int endPos = StringFind(json, "}", pos);
      if(endPos < 0) break;

      string cmdBlock = StringSubstr(json, pos, endPos - pos + 1);
      ExecuteLimitOrderBlock(cmdBlock);
      pos = endPos + 1;
   }

   // 3. Check for ORDER_CANCEL commands
   pos = 0;
   while((pos = StringFind(json, "{\"action\":\"ORDER_CANCEL\"", pos)) >= 0)
   {
      int endPos = StringFind(json, "}", pos);
      if(endPos < 0) break;

      string cmdBlock = StringSubstr(json, pos, endPos - pos + 1);
      ExecuteCancelOrderBlock(cmdBlock);
      pos = endPos + 1;
   }
}

//+------------------------------------------------------------------+
//| Execute a single Limit Order from JSON block                     |
//+------------------------------------------------------------------+
void ExecuteLimitOrderBlock(string block)
{
   string symbol = ExtractJsonField(block, "\"symbol\":\"", "\"");
   string typeStr = ExtractJsonField(block, "\"type\":\"", "\"");
   double price   = StringToDouble(ExtractJsonField(block, "\"price\":", ","));
   double sl      = StringToDouble(ExtractJsonField(block, "\"sl\":", ","));
   double tp      = StringToDouble(ExtractJsonField(block, "\"tp\":", ","));
   double lots    = StringToDouble(ExtractJsonField(block, "\"lots\":", ","));

   if(symbol == "" || price <= 0 || lots <= 0) return;

   // Normalize symbol if suffix exists (e.g. EURUSD -> EURUSD.r or EURUSD_oanda)
   if(!SymbolInfoInteger(symbol, SYMBOL_CUSTOM) && !SymbolSelect(symbol, true))
   {
      // Try current chart symbol if base matches
      if(StringFind(_Symbol, symbol) >= 0)
         symbol = _Symbol;
   }

   double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   lots = MathMax(minLot, MathMin(maxLot, MathFloor(lots / lotStep) * lotStep));

   bool success = false;
   if(typeStr == "BUY_LIMIT")
   {
      success = trade.BuyLimit(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, "SMC_Bot");
   }
   else if(typeStr == "SELL_LIMIT")
   {
      success = trade.SellLimit(lots, price, symbol, sl, tp, ORDER_TIME_GTC, 0, "SMC_Bot");
   }

   if(success)
   {
      PrintFormat("✅ Order placed: %s %s %.2f lots @ %.5f | SL: %.5f | TP: %.5f", typeStr, symbol, lots, price, sl, tp);
   }
   else
   {
      PrintFormat("❌ Order failed for %s: %s (code %d)", symbol, trade.ResultRetcodeDescription(), trade.ResultRetcode());
   }
}

//+------------------------------------------------------------------+
//| Execute Order Cancel from JSON block                             |
//+------------------------------------------------------------------+
void ExecuteCancelOrderBlock(string block)
{
   string orderIdStr = ExtractJsonField(block, "\"order_id\":\"", "\"");
   ulong ticket = (ulong)StringToInteger(orderIdStr);
   if(ticket > 0)
   {
      trade.OrderDelete(ticket);
   }
}

//+------------------------------------------------------------------+
//| Close all open trades and cancel all pending orders              |
//+------------------------------------------------------------------+
void CloseAllPositionsAndOrders()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0)
         trade.PositionClose(ticket);
   }

   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket > 0)
         trade.OrderDelete(ticket);
   }
   Print("All open positions and pending orders closed.");
}

//+------------------------------------------------------------------+
//| Helper to extract value between two markers                      |
//+------------------------------------------------------------------+
string ExtractJsonField(string source, string startMarker, string endMarker)
{
   int start = StringFind(source, startMarker);
   if(start < 0) return "";
   start += StringLen(startMarker);

   int end = StringFind(source, endMarker, start);
   if(end < 0)
   {
      // If end marker not found, check closing bracket
      end = StringFind(source, "}", start);
      if(end < 0) end = StringLen(source);
   }

   return StringSubstr(source, start, end - start);
}

