//+------------------------------------------------------------------+
//| TradeIdeaExecutor.mq5                                        |
//| Execution EA — connects to Python trade manager over TCP         |
//|                                                                  |
//| Attach to any chart, enable Algo Trading, set Python host/port. |
//| Python side: EXECUTION_BACKEND=ea                               |
//+------------------------------------------------------------------+
#property copyright "Trade Manager"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>
#include <TradeIdea/Protocol.mqh>

input string InpPythonHost      = "127.0.0.1";
input int    InpPythonPort      = 19520;
input int    InpMagicNumber     = 234000;
input int    InpDeviation       = 20;
input int    InpTimerMs         = 100;
input bool   InpEnableLogging   = true;

int      g_socket = INVALID_HANDLE;
CTrade   g_trade;
string   g_recv_buffer = "";
datetime g_last_heartbeat = 0;

//+------------------------------------------------------------------+
bool TiLog(const string msg)
  {
   if(!InpEnableLogging)
      return true;
   Print("[TradeIdeaExecutor] ", msg);
   return true;
  }

//+------------------------------------------------------------------+
bool TiEnsureConnected()
  {
   if(g_socket != INVALID_HANDLE && SocketIsConnected(g_socket))
      return true;

   if(g_socket != INVALID_HANDLE)
     {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
     }

   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE)
     {
      TiLog("SocketCreate failed");
      return false;
     }

   if(!SocketConnect(g_socket, InpPythonHost, InpPythonPort, 5000))
     {
      TiLog(StringFormat("SocketConnect %s:%d failed err=%d",
                         InpPythonHost, InpPythonPort, GetLastError()));
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      return false;
     }

   string hello = StringFormat(
      "{\"type\":\"%s\",\"magic\":%d,\"terminal\":\"%s\",\"account\":%I64d}",
      TI_EVT_CONNECTED,
      InpMagicNumber,
      TiEscape(TerminalInfoString(TERMINAL_NAME)),
      AccountInfoInteger(ACCOUNT_LOGIN)
   );
   TiSendJson(g_socket, hello);
   TiLog(StringFormat("Connected to Python %s:%d", InpPythonHost, InpPythonPort));
   return true;
  }

//+------------------------------------------------------------------+
string TiReadLine()
  {
   if(g_socket == INVALID_HANDLE)
      return "";
   uchar chunk[];
   ArrayResize(chunk, 512);
   int read = SocketRead(g_socket, chunk, 512, 20);
   if(read > 0)
      g_recv_buffer += CharArrayToString(chunk, 0, read, CP_UTF8);

   int nl = StringFind(g_recv_buffer, "\n");
   if(nl < 0)
      return "";

   string line = StringSubstr(g_recv_buffer, 0, nl);
   g_recv_buffer = StringSubstr(g_recv_buffer, nl + 1);
   StringTrimRight(line);
   return line;
  }

//+------------------------------------------------------------------+
bool TiSendTick(const int socket, const string id, const string symbol)
  {
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
      return TiSendErr(socket, id, GetLastError(), "symbol tick unavailable");
   string extra = StringFormat(",\"bid\":%.10f,\"ask\":%.10f", tick.bid, tick.ask);
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
bool TiSendSymbolSpec(const int socket, const string id, const string symbol)
  {
   if(!SymbolSelect(symbol, true))
      return TiSendErr(socket, id, GetLastError(), "symbol select failed");
   string extra = StringFormat(
      ",\"trade_tick_size\":%.10f,\"trade_tick_value\":%.10f,"
      "\"volume_min\":%.4f,\"volume_max\":%.4f,\"volume_step\":%.4f,\"digits\":%d",
      SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE),
      SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE),
      SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN),
      SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX),
      SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP),
      (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)
   );
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
bool TiSendAccountInfo(const int socket, const string id)
  {
   string extra = StringFormat(
      ",\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,"
      "\"login\":%I64d,\"leverage\":%d",
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN),
      AccountInfoInteger(ACCOUNT_LOGIN),
      (int)AccountInfoInteger(ACCOUNT_LEVERAGE)
   );
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
string TiPositionJson(const ulong ticket)
  {
   if(!PositionSelectByTicket(ticket))
      return "";
   return StringFormat(
      "{\"ticket\":%I64u,\"identifier\":%I64u,\"magic\":%d,"
      "\"volume\":%.4f,\"price_open\":%.10f,\"sl\":%.10f,\"tp\":%.10f,"
      "\"type\":%d,\"symbol\":\"%s\"}",
      ticket,
      PositionGetInteger(POSITION_IDENTIFIER),
      (int)PositionGetInteger(POSITION_MAGIC),
      PositionGetDouble(POSITION_VOLUME),
      PositionGetDouble(POSITION_PRICE_OPEN),
      PositionGetDouble(POSITION_SL),
      PositionGetDouble(POSITION_TP),
      (int)PositionGetInteger(POSITION_TYPE),
      TiEscape(PositionGetString(POSITION_SYMBOL))
   );
  }

//+------------------------------------------------------------------+
bool TiSendPositions(const int socket, const string id, const string symbol, const int magic)
  {
   string arr = "[";
   int count = 0;
   int total = PositionsTotal();
   for(int i = total - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != magic)
         continue;
      if(symbol != "" && PositionGetString(POSITION_SYMBOL) != symbol)
         continue;
      string pj = TiPositionJson(ticket);
      if(pj == "")
         continue;
      if(count > 0)
         arr += ",";
      arr += pj;
      count++;
     }
   arr += "]";
   string extra = ",\"positions\":" + arr;
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
bool TiSelectOrderByTicket(const ulong ticket)
  {
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
     {
      ulong t = OrderGetTicket(i);
      if(t == ticket)
         return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
string TiOrderJson(const ulong ticket)
  {
   if(!TiSelectOrderByTicket(ticket))
      return "";
   return StringFormat(
      "{\"ticket\":%I64u,\"magic\":%d,\"volume\":%.4f,\"volume_current\":%.4f,"
      "\"price_open\":%.10f,\"price\":%.10f,\"type\":%d,\"symbol\":\"%s\"}",
      ticket,
      (int)OrderGetInteger(ORDER_MAGIC),
      OrderGetDouble(ORDER_VOLUME_INITIAL),
      OrderGetDouble(ORDER_VOLUME_CURRENT),
      OrderGetDouble(ORDER_PRICE_OPEN),
      OrderGetDouble(ORDER_PRICE_OPEN),
      (int)OrderGetInteger(ORDER_TYPE),
      TiEscape(OrderGetString(ORDER_SYMBOL))
   );
  }

//+------------------------------------------------------------------+
bool TiFillViolatesEntry(const string direction, const double entry,
                         const double fill, const double tick_size)
  {
   double tol = tick_size * 3.0;
   if(direction == "BUY")
     {
      if(fill < entry - tol)
         return true;
      if(fill > entry + tol * 5.0)
         return true;
      return false;
     }
   if(fill > entry + tol)
      return true;
   if(fill < entry - tol * 5.0)
      return true;
   return false;
  }

//+------------------------------------------------------------------+
bool TiEmergencyClosePosition(const string symbol, const ulong ticket,
                              const string direction, const double volume)
  {
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpDeviation);
   if(g_trade.PositionClose(ticket))
      return true;
   TiLog(StringFormat("Bad-fill close failed ticket=%I64u err=%d %s",
                      ticket, g_trade.ResultRetcode(), g_trade.ResultComment()));
   return false;
  }

//+------------------------------------------------------------------+
bool TiVerifyPendingResting(const string symbol, const ulong order_ticket,
                            const string direction, const double entry,
                            const double volume, const double tick_size,
                            string &reason)
  {
   Sleep(200);
   if(TiSelectOrderByTicket(order_ticket))
     {
      reason = "pending";
      return true;
     }

   int total = PositionsTotal();
   for(int i = total - 1; i >= 0; i--)
     {
      ulong pt = PositionGetTicket(i);
      if(pt == 0)
         continue;
      if((int)PositionGetInteger(POSITION_MAGIC) != InpMagicNumber)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol)
         continue;
      if(MathAbs(PositionGetDouble(POSITION_VOLUME) - volume) > 0.0001)
         continue;
      double fill = PositionGetDouble(POSITION_PRICE_OPEN);
      if(TiFillViolatesEntry(direction, entry, fill, tick_size))
        {
         TiEmergencyClosePosition(symbol, pt, direction, volume);
         reason = StringFormat("immediate_bad_fill@%s", DoubleToString(fill, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS)));
         return false;
        }
     }
   reason = "not_pending_no_bad_position";
   return false;
  }

//+------------------------------------------------------------------+
bool TiSendOrders(const int socket, const string id, const string symbol, const int magic)
  {
   string arr = "[";
   int count = 0;
   int total = OrdersTotal();
   for(int i = total - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if((int)OrderGetInteger(ORDER_MAGIC) != magic)
         continue;
      if(symbol != "" && OrderGetString(ORDER_SYMBOL) != symbol)
         continue;
      string oj = TiOrderJson(ticket);
      if(oj == "")
         continue;
      if(count > 0)
         arr += ",";
      arr += oj;
      count++;
     }
   arr += "]";
   return TiSendOk(socket, id, ",\"orders\":" + arr);
  }

//+------------------------------------------------------------------+
bool TiHandlePlacePending(const int socket, const string id, const string json)
  {
   string symbol = TiJsonGetString(json, "symbol");
   string direction = TiJsonGetString(json, "direction");
   double volume = TiJsonGetDouble(json, "volume");
   double entry = TiJsonGetDouble(json, "entry");
   double sl = TiJsonGetDouble(json, "sl");
   double tp = TiJsonGetDouble(json, "tp");
   double tick_size = TiJsonGetDouble(json, "tick_size");
   int order_type = (int)TiJsonGetLong(json, "order_type");
   if(tick_size <= 0.0)
      tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);

   if(!SymbolSelect(symbol, true))
      return TiSendErr(socket, id, GetLastError(), "symbol unavailable");

   const int max_attempts = 3;
   for(int attempt = 1; attempt <= max_attempts; attempt++)
     {
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action       = TRADE_ACTION_PENDING;
      req.symbol       = symbol;
      req.volume       = volume;
      req.type         = (ENUM_ORDER_TYPE)order_type;
      req.price        = entry;
      req.sl           = sl;
      req.tp           = tp;
      req.deviation    = InpDeviation;
      req.magic        = InpMagicNumber;
      req.comment      = "TradeIdeaBot_Pending";
      req.type_time    = ORDER_TIME_GTC;
      req.type_filling = ORDER_FILLING_RETURN;

      if(!OrderSend(req, res))
        {
         if(attempt < max_attempts)
           {
            Sleep((ulong)(500 * attempt));
            continue;
           }
         return TiSendErr(socket, id, res.retcode, res.comment);
        }
      if(res.retcode != TI_TRADE_RETCODE_DONE)
        {
         if(attempt < max_attempts)
           {
            Sleep((ulong)(500 * attempt));
            continue;
           }
         return TiSendErr(socket, id, res.retcode, res.comment);
        }

      string verify_reason = "";
      if(!TiVerifyPendingResting(symbol, res.order, direction, entry, volume, tick_size, verify_reason))
        {
         if(TiSelectOrderByTicket(res.order))
            g_trade.OrderDelete(res.order);
         TiLog(StringFormat("Pending verify failed: %s", verify_reason));
         if(attempt < max_attempts)
           {
            Sleep((ulong)(500 * attempt));
            continue;
           }
         return TiSendErr(socket, id, 0, verify_reason);
        }

      string extra = StringFormat(",\"retcode\":%I64d,\"order\":%I64u,\"comment\":\"%s\"",
                                  res.retcode, res.order, TiEscape(res.comment));
      return TiSendOk(socket, id, extra);
     }
   return TiSendErr(socket, id, 0, "place pending exhausted retries");
  }

//+------------------------------------------------------------------+
bool TiHandleCancel(const int socket, const string id, const string json)
  {
   ulong ticket = (ulong)TiJsonGetLong(json, "ticket");
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   if(!g_trade.OrderDelete(ticket))
      return TiSendErr(socket, id, g_trade.ResultRetcode(), g_trade.ResultComment());
   return TiSendOk(socket, id, StringFormat(",\"retcode\":%I64d", g_trade.ResultRetcode()));
  }

//+------------------------------------------------------------------+
bool TiHandleModify(const int socket, const string id, const string json)
  {
   ulong ticket = (ulong)TiJsonGetLong(json, "ticket");
   double sl = TiJsonGetDouble(json, "sl");
   double tp = TiJsonGetDouble(json, "tp");
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   if(!g_trade.PositionModify(ticket, sl, tp))
     {
      long rc = g_trade.ResultRetcode();
      if(rc == 10025) // TRADE_RETCODE_NO_CHANGES
         return TiSendOk(socket, id, ",\"retcode\":10025");
      return TiSendErr(socket, id, rc, g_trade.ResultComment());
     }
   return TiSendOk(socket, id, StringFormat(",\"retcode\":%I64d", g_trade.ResultRetcode()));
  }

//+------------------------------------------------------------------+
bool TiHandleClose(const int socket, const string id, const string json)
  {
   ulong ticket = (ulong)TiJsonGetLong(json, "ticket");
   string symbol = TiJsonGetString(json, "symbol");
   string direction = TiJsonGetString(json, "direction");
   double volume = TiJsonGetDouble(json, "volume");

   if(!SymbolSelect(symbol, true))
      return TiSendErr(socket, id, GetLastError(), "symbol unavailable");

   const int max_attempts = 3;
   for(int attempt = 1; attempt <= max_attempts; attempt++)
     {
      MqlTick tick;
      if(!SymbolInfoTick(symbol, tick))
         return TiSendErr(socket, id, GetLastError(), "symbol tick unavailable");

      ENUM_ORDER_TYPE close_type;
      double price;
      if(direction == "BUY")
        {
         close_type = ORDER_TYPE_SELL;
         price = tick.bid;
        }
      else
        {
         close_type = ORDER_TYPE_BUY;
         price = tick.ask;
        }

      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action       = TRADE_ACTION_DEAL;
      req.symbol       = symbol;
      req.volume       = volume;
      req.type         = close_type;
      req.position     = ticket;
      req.price        = price;
      req.deviation    = InpDeviation;
      req.magic        = InpMagicNumber;
      req.comment      = "TradeIdeaBot_Close";
      req.type_time    = ORDER_TIME_GTC;
      req.type_filling = ORDER_FILLING_IOC;

      if(!OrderSend(req, res))
        {
         TiLog(StringFormat("Close attempt %d failed send: %I64d %s",
                            attempt, res.retcode, res.comment));
         if(attempt < max_attempts)
           {
            Sleep((ulong)(500 * attempt));
            continue;
           }
         return TiSendErr(socket, id, res.retcode, res.comment);
        }
      if(res.retcode != TI_TRADE_RETCODE_DONE)
        {
         TiLog(StringFormat("Close attempt %d retcode=%I64d %s",
                            attempt, res.retcode, res.comment));
         if(attempt < max_attempts)
           {
            Sleep((ulong)(500 * attempt));
            continue;
           }
         return TiSendErr(socket, id, res.retcode, res.comment);
        }

      return TiSendOk(socket, id, StringFormat(",\"retcode\":%I64d,\"price\":%.10f",
                                                 res.retcode, res.price));
     }
   return TiSendErr(socket, id, 0, "close exhausted retries");
  }

//+------------------------------------------------------------------+
bool TiHandleGetOrderHistory(const int socket, const string id, const string json)
  {
   ulong order_ticket = (ulong)TiJsonGetLong(json, "order_ticket");
   string hist_part = "null";
   string deals_arr = "[";

   if(HistorySelect(0, TimeCurrent()))
     {
      if(HistoryOrderSelect(order_ticket))
        {
         long state = HistoryOrderGetInteger(order_ticket, ORDER_STATE);
         hist_part = StringFormat(
            "{\"ticket\":%I64u,\"state\":%I64d,\"comment\":\"%s\",\"magic\":%I64d}",
            order_ticket,
            state,
            TiEscape(HistoryOrderGetString(order_ticket, ORDER_COMMENT)),
            HistoryOrderGetInteger(order_ticket, ORDER_MAGIC)
         );
        }

      int deal_count = 0;
      int deals = HistoryDealsTotal();
      for(int i = 0; i < deals; i++)
        {
         ulong deal = HistoryDealGetTicket(i);
         if(deal == 0)
            continue;
         if((ulong)HistoryDealGetInteger(deal, DEAL_ORDER) != order_ticket)
            continue;
         if(deal_count > 0)
            deals_arr += ",";
         deals_arr += StringFormat(
            "{\"ticket\":%I64u,\"price\":%.10f,\"entry\":%I64d,"
            "\"position_id\":%I64u,\"time\":%I64d}",
            deal,
            HistoryDealGetDouble(deal, DEAL_PRICE),
            HistoryDealGetInteger(deal, DEAL_ENTRY),
            HistoryDealGetInteger(deal, DEAL_POSITION_ID),
            HistoryDealGetInteger(deal, DEAL_TIME)
         );
         deal_count++;
        }
     }
   deals_arr += "]";
   string extra = StringFormat(",\"history_order\":%s,\"deals\":%s", hist_part, deals_arr);
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
bool TiHandleCloseDetails(const int socket, const string id, const string json)
  {
   ulong ticket = (ulong)TiJsonGetLong(json, "ticket");
   if(!HistorySelect(0, TimeCurrent()))
      return TiSendErr(socket, id, GetLastError(), "history select failed");

   double profit = 0, commission = 0, swap = 0;
   double close_price = 0;
   datetime last_time = 0;
   int deals = HistoryDealsTotal();
   for(int i = deals - 1; i >= 0; i--)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0)
         continue;
      if((ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID) != ticket)
         continue;
      if((int)HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
         continue;
      datetime t = (datetime)HistoryDealGetInteger(deal, DEAL_TIME);
      if(t >= last_time)
        {
         last_time = t;
         close_price = HistoryDealGetDouble(deal, DEAL_PRICE);
        }
      profit     += HistoryDealGetDouble(deal, DEAL_PROFIT);
      commission += HistoryDealGetDouble(deal, DEAL_COMMISSION);
      swap       += HistoryDealGetDouble(deal, DEAL_SWAP);
     }

   if(last_time == 0)
      return TiSendErr(socket, id, 0, "no exit deals");

   string extra = StringFormat(
      ",\"close_price\":%.10f,\"profit\":%.2f,\"commission\":%.2f,\"swap\":%.2f",
      close_price, profit, commission, swap
   );
   return TiSendOk(socket, id, extra);
  }

//+------------------------------------------------------------------+
void TiDispatchCommand(const string json)
  {
   if(g_socket == INVALID_HANDLE)
      return;

   string cmd = TiJsonGetType(json);
   string id  = TiJsonGetId(json);

   if(cmd == "PING")
     {
      TiSendOk(g_socket, id);
      return;
     }
   if(cmd == "GET_TICK")
     {
      TiSendTick(g_socket, id, TiJsonGetString(json, "symbol"));
      return;
     }
   if(cmd == "GET_SYMBOL_SPEC")
     {
      TiSendSymbolSpec(g_socket, id, TiJsonGetString(json, "symbol"));
      return;
     }
   if(cmd == "GET_ACCOUNT_INFO")
     {
      TiSendAccountInfo(g_socket, id);
      return;
     }
   if(cmd == "GET_POSITIONS")
     {
      TiSendPositions(g_socket, id, TiJsonGetString(json, "symbol"), (int)TiJsonGetLong(json, "magic"));
      return;
     }
   if(cmd == "GET_ORDERS")
     {
      TiSendOrders(g_socket, id, TiJsonGetString(json, "symbol"), (int)TiJsonGetLong(json, "magic"));
      return;
     }
   if(cmd == "GET_POSITION")
     {
      ulong ticket = (ulong)TiJsonGetLong(json, "ticket");
      string pj = TiPositionJson(ticket);
      if(pj == "")
         TiSendErr(g_socket, id, 0, "position not found");
      else
         TiSendOk(g_socket, id, ",\"position\":" + pj);
      return;
     }
   if(cmd == "PLACE_PENDING")
     {
      TiHandlePlacePending(g_socket, id, json);
      return;
     }
   if(cmd == "CANCEL_ORDER")
     {
      TiHandleCancel(g_socket, id, json);
      return;
     }
   if(cmd == "MODIFY_POSITION")
     {
      TiHandleModify(g_socket, id, json);
      return;
     }
   if(cmd == "CLOSE_POSITION")
     {
      TiHandleClose(g_socket, id, json);
      return;
     }
   if(cmd == "GET_CLOSE_DETAILS")
     {
      TiHandleCloseDetails(g_socket, id, json);
      return;
     }
   if(cmd == "GET_ORDER_HISTORY")
     {
      TiHandleGetOrderHistory(g_socket, id, json);
      return;
     }

   TiSendErr(g_socket, id, 0, "unknown command");
  }

//+------------------------------------------------------------------+
void TiSendTradeEvent(const string event_name, const ulong ticket,
                      const string symbol, const double price, const double profit = 0.0)
  {
   if(g_socket == INVALID_HANDLE)
      return;
   string body = StringFormat(
      "{\"type\":\"%s\",\"event\":\"%s\",\"ticket\":%I64u,"
      "\"symbol\":\"%s\",\"price\":%.10f,\"profit\":%.2f,\"magic\":%d}",
      TI_EVT_TRADE, event_name, ticket, TiEscape(symbol), price, profit, InpMagicNumber
   );
   TiSendJson(g_socket, body);
  }

//+------------------------------------------------------------------+
int OnInit()
  {
   g_trade.SetExpertMagicNumber(InpMagicNumber);
   g_trade.SetDeviationInPoints(InpDeviation);
   EventSetMillisecondTimer(InpTimerMs);
   if(!TiEnsureConnected())
      TiLog("Will retry connection on timer");
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_socket != INVALID_HANDLE)
     {
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
     }
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   if(!TiEnsureConnected())
      return;

   // heartbeat every 30s
   if(TimeCurrent() - g_last_heartbeat >= 30)
     {
      string hb = StringFormat("{\"type\":\"%s\",\"ts\":%I64d}", TI_EVT_HEARTBEAT, TimeCurrent());
      TiSendJson(g_socket, hb);
      g_last_heartbeat = TimeCurrent();
     }

   for(int i = 0; i < 20; i++)
     {
      string line = TiReadLine();
      if(line == "")
         break;
      TiDispatchCommand(line);
     }
  }

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.symbol == "")
      return;

   if(trans.type == TRADE_TRANSACTION_ORDER_ADD)
     {
      if(HistoryOrderSelect(trans.order))
        {
         if((int)HistoryOrderGetInteger(trans.order, ORDER_MAGIC) == InpMagicNumber)
            TiSendTradeEvent("ORDER_ADD", trans.order, trans.symbol, trans.price);
        }
     }
   else if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
     {
      if(HistoryDealSelect(trans.deal))
        {
         if((int)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagicNumber)
            return;
         string ev = "DEAL_ADD";
         if((int)HistoryDealGetInteger(trans.deal, DEAL_ENTRY) == DEAL_ENTRY_IN)
            ev = "POSITION_OPENED";
         else if((int)HistoryDealGetInteger(trans.deal, DEAL_ENTRY) == DEAL_ENTRY_OUT)
            ev = "POSITION_CLOSED";
         TiSendTradeEvent(ev, trans.deal, trans.symbol, trans.price,
                          HistoryDealGetDouble(trans.deal, DEAL_PROFIT));
        }
     }
  }

//+------------------------------------------------------------------+
