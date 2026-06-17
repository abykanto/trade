# 🗺️ Visual Map: How Your Trading System Works

Hello! I have redesigned this guide to be highly visual. We will use small diagrams and short bullet points to show how an idea moves through the system.

***

## 1️⃣ The Signal Arrives
How a new trading idea gets into the system.

* 📡 **External Source** sends a signal to your API.
* 🛡️ **API checks math** (e.g., is Stop Loss below Buy Price?).
* 💾 **Saves to Database** as a new idea.

```mermaid
flowchart LR
    A["📡 Signal Source"] -->|"Send Data"| B("🛡️ API Server")
    B -->|"Check Math"| C[("💾 Database")]
    C --> D["Status: WAITING FOR SETUP"]
```

***

## 2️⃣ Checking Risk & Sizing
Before doing anything, the bot makes sure the trade is safe.

* 📉 **Checks Daily Loss** (Have we lost too much today?).
* 📏 **Calculates Lot Size** based on your exact Risk Budget.

```mermaid
flowchart TD
    A["Status: WAITING FOR SETUP"] --> B{"📉 Daily Loss Limit OK?"}
    B -- Yes --> C["📏 Calculate Lot Size"]
    B -- No --> D["❌ Reject Idea"]
```

***

## 3️⃣ Placing the Order (Zero Latency)
The bot hands the job over to MT5 immediately.

* ⚡ **Sends Pending Order** directly to MT5.
* 👁️ **Monitors** MT5 to see if it fills.

```mermaid
flowchart LR
    A["📏 Lot Size Ready"] -->|"Send Limit/Stop Order"| B("⚡ MT5 Broker")
    B --> C["Status: PENDING ORDER PLACED"]
    C -->|"Price hits entry"| D(("🟢 TRADE OPEN"))
```

***

## 4️⃣ Active Trade Management
Once the trade is open, the bot protects your profit.

* 🛡️ **Trailing Stop** moves up to lock in profit.
* 🎯 **Take Profit** closes for a win.
* 🛑 **Stop Loss** closes for a loss.

```mermaid
flowchart TD
    A(("🟢 TRADE OPEN")) --> B{"Price Moves"}
    B -- 📈 Into Profit --> C["🛡️ Trailing Stop Locks Profit"]
    C --> A
    B -- 🎯 Hits Target --> D(["🏆 TP REACHED"])
    B -- 🛑 Hits Stop Loss --> E(["💔 EARLY EXIT"])
```

***

## 5️⃣ Whipsaw Recovery (The Safety Net)
What happens if the trade hits the Stop Loss?

* 🧮 **Calculate Loss:** How many dollars did we lose?
* 💰 **Check Budget:** Do we still have money left in the idea's Risk Budget?
* 🔄 **Try Again:** If budget remains, try the trade again.
* ☠️ **Permanent Kill:** If budget is empty, kill the idea forever.

```mermaid
flowchart TD
    A(["💔 EARLY EXIT"]) --> B["🧮 Add Loss to Consumed Risk"]
    B --> C{"💰 Is Consumed Risk < Max Budget?"}
    C -- Yes (Budget Remains) --> D["🔄 WAITING FOR REENTRY"]
    D -->|"Send New Order"| E("⚡ MT5 Broker")
    C -- No (Budget Empty) --> F(["☠️ IDEA INVALIDATED"])
```
