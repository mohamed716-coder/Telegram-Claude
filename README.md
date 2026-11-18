# Telegram Shop Bot

A Telegram bot that sells digital products (eSIMs, codes, files…) from a tree of labels,
with an owner panel, admin panels, customer balances topped up through Binance, and
earnings/withdrawals for sellers.

Written in Python with [python-telegram-bot](https://python-telegram-bot.org) 22.8 and SQLite
(a single file, `data/shop.db`), so it runs on any small server or PC.

## Setup

1. **Create the bot.** In Telegram open [@BotFather](https://t.me/BotFather), send `/newbot`,
   choose a name and username, and copy the token it gives you.
2. **Install Python 3.10 or newer**, then in this folder:

   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure.** Copy `.env.example` to `.env` and paste your token:

   ```
   BOT_TOKEN=123456789:AA...your token...
   ```

   The owner ID (`6883493877`) and the Binance Pay ID (`850940840`) are already the
   defaults; change them in `.env` only if they ever change.

4. **Run it:**

   ```bash
   python bot.py
   ```

   Open your bot in Telegram and press **Start**. Keep the program running for the bot to
   answer (on a server, run it with `systemd`, `screen` or `tmux`).

On first run the bot sets its own description ("Press /start to start the shop") and the
command list shown by the **Menu** button. The owner and admins see their extra commands
in that list.

## Panels

| Owner (ID 6883493877) | Admin | Customer |
|---|---|---|
| 🛍 Shop | 🛍 Shop | 🛍 Shop |
| 📦 My Products | 📦 My Products (own products) | 💳 Add Balance |
| ➕ Add Product | ➕ Add Product | 🧾 My Orders |
| 🏷 Add Label | 📊 Earnings (own) | |
| 📊 Earnings (everyone) | 💳 Add Balance | |
| 💳 Add Balance | | |

`/start` greets with **Welcome Owner**, **Welcome Admin**, or **Welcome _first name_**, and
shows the balance.

## How the shop works

### 1. Labels (owner: 🏷 Add Label)

Labels nest as deep as you like. The **last label of a branch is the product** people buy:

```
Esim
└── Oman
    ├── Omantel
    │   ├── 5GB   ← product (has a price and stock)
    │   └── 10GB  ← product
    └── Vodafone
        └── 20GB  ← product
```

- Open a label and press **➕ New label inside …**, then send the name. Send several names
  at once, one per line (e.g. `5GB` `10GB` `20GB` on separate lines).
- Open a product label to **💲 Set price**, **✏️ Rename** or **🗑 Delete** it.
- Products without a price are hidden from customers.

### 2. Adding products (➕ Add Product)

Open the labels down to the product, then **send one message per product** (text, photo,
file, video…). Each message is delivered to the buyer exactly as you sent it. Press the
green **Done** to save them all (or red **Cancel**). Every product gets a unique
**product ID**, e.g. `K7Q2M9XA`.

### 3. My Products (📦)

Browse like the shop; a product shows the IDs of its products in stock. Tap an ID to see
what's inside, then:

- **🗑 Remove**: deletes it from the shop.
- **📤 Pull**: takes it out of the shop and sends it to you as if you bought it.

The owner sees and manages everyone's products; an admin only their own.
**📥 Download sales analysis** sends a spreadsheet (CSV) with sold / left / revenue per
product and per seller.

### 4. Buying

Customers open 🛍 Shop, pick a product and a quantity, and confirm. The price comes off
their balance and the products are sent right away. The money goes to the **earnings** of
whoever added each product (owner or admin).

### 5. Adding balance (💳 Add Balance)

Pick 10 / 20 / 30 / 50 / 100 $ or **Custom** (whole numbers only), send the money to
Binance Pay ID **850940840**, send a **photo of the receipt**, and press **Confirm**. The
owner gets the receipt with **Approve** / **Reject** buttons; on approval the balance is
added.

**Sellers** get **10% off** every product. Anyone whose total deposits reach **1000$**
becomes a seller automatically, or the owner can use `/seller ID`.

### 6. Earnings and withdrawals

📊 **Earnings** shows the owner's earnings, each admin's earnings, the total earnings and
the total purchases. Admins see their own. `/withdraw` asks for an amount and a Binance ID;
the amount leaves your earnings straight away and the owner gets **Approve** / **Reject**
buttons (if rejected, the money returns to the admin's earnings). The owner's own
withdrawals are recorded immediately.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Open the menu |
| `/help` | everyone | List the commands for your panel |
| `/cancel` | everyone | Cancel what you're doing |
| `/withdraw` | owner, admins | Withdraw earnings to Binance |
| `/addbal ID AMOUNT` | owner | Add balance to a user (counts as a deposit) |
| `/modifybal ID` | owner | Show a user's balance and set a new one |
| `/addadmin ID` / `/removeadmin ID` | owner | Give / remove the admin panel |
| `/seller ID` / `/removeseller ID` | owner | Give / remove the 10% seller discount |
| `/ban ID` / `/unban ID` | owner | Block / unblock a user from the bot |
| `/close` / `/open` | owner | Close / open the shop for customers |
| `/status` | owner | All users: ID, name, balance, total deposits (+ CSV file) |

Commands that need a user also work without arguments: e.g. send `/ban` and the bot asks
for the ID.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Token from @BotFather (required) |
| `OWNER_ID` | `6883493877` | Telegram ID of the owner |
| `BINANCE_PAY_ID` | `850940840` | Where customers send deposits |
| `DB_PATH` | `data/shop.db` | Database file |
| `SELLER_DISCOUNT_PERCENT` | `10` | Seller discount |
| `SELLER_AUTO_THRESHOLD` | `1000` | Total deposits ($) that make someone a seller |

Back up `data/shop.db` regularly: it holds all products, balances and orders.

## Tests

The tests run the real bot against a fake Telegram server, so no token is needed:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
