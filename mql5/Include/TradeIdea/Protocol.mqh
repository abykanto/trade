//+------------------------------------------------------------------+
//| Protocol.mqh — JSON-line helpers for Trade Manager Python bridge |
//+------------------------------------------------------------------+
#ifndef TRADEIDEA_PROTOCOL_MQH
#define TRADEIDEA_PROTOCOL_MQH

#define TI_RSP_OK           "OK"
#define TI_RSP_ERR          "ERR"
#define TI_EVT_CONNECTED    "CONNECTED"
#define TI_EVT_HEARTBEAT    "HEARTBEAT"
#define TI_EVT_TRADE        "TRADE_EVENT"

#define TI_TRADE_RETCODE_DONE 10009

//--- extract "key":"value" string field
string TiJsonGetString(const string json, const string key)
  {
   string pattern = "\"" + key + "\":\"";
   int start = StringFind(json, pattern);
   if(start < 0)
      return "";
   start += StringLen(pattern);
   int end = StringFind(json, "\"", start);
   if(end < 0)
      return "";
   return StringSubstr(json, start, end - start);
  }

//--- extract numeric field (unquoted)
double TiJsonGetDouble(const string json, const string key)
  {
   string pattern = "\"" + key + "\":";
   int start = StringFind(json, pattern);
   if(start < 0)
      return 0.0;
   start += StringLen(pattern);
   int end = start;
   int len = StringLen(json);
   while(end < len)
     {
      ushort ch = StringGetCharacter(json, end);
      if(ch == ',' || ch == '}' || ch == ']')
         break;
      end++;
     }
   string raw = StringSubstr(json, start, end - start);
   StringTrimLeft(raw);
   StringTrimRight(raw);
   return StringToDouble(raw);
  }

long TiJsonGetLong(const string json, const string key)
  {
   return (long)TiJsonGetDouble(json, key);
  }

string TiJsonGetType(const string json)
  {
   return TiJsonGetString(json, "type");
  }

string TiJsonGetId(const string json)
  {
   return TiJsonGetString(json, "id");
  }

//--- send one JSON line terminated by newline
bool TiSendJson(const int socket, const string json)
  {
   string line = json + "\n";
   uchar data[];
   int n = StringToCharArray(line, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n <= 0)
      return false;
   return SocketSend(socket, data, n - 1) == n - 1;
  }

bool TiSendOk(const int socket, const string id, const string extra = "")
  {
   string body = "{\"type\":\"" + TI_RSP_OK + "\",\"id\":\"" + id + "\"";
   if(extra != "")
      body += extra;
   body += "}";
   return TiSendJson(socket, body);
  }

bool TiSendErr(const int socket, const string id, const long retcode, const string error)
  {
   string body = StringFormat(
      "{\"type\":\"%s\",\"id\":\"%s\",\"retcode\":%I64d,\"error\":\"%s\"}",
      TI_RSP_ERR, id, retcode, error
   );
   return TiSendJson(socket, body);
  }

//--- read until newline (blocking with timeout per call)
string TiRecvLine(const int socket, const int timeout_ms = 50)
  {
   static string buffer = "";
   uchar chunk[];
   ArrayResize(chunk, 256);
   int read = SocketRead(socket, chunk, 256, timeout_ms);
   if(read > 0)
     {
      buffer += CharArrayToString(chunk, 0, read, CP_UTF8);
     }
   int nl = StringFind(buffer, "\n");
   if(nl < 0)
      return "";
   string line = StringSubstr(buffer, 0, nl);
   buffer = StringSubstr(buffer, nl + 1);
   StringTrimRight(line);
   return line;
  }

string TiEscape(const string value)
  {
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
  }

#endif
