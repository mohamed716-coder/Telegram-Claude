# Telegram Shop Bot

A Telegram bot that sells digital products (eSIMs, codes, files…) from a tree of labels,
with an owner panel, admin panels, customer balances topped up through Binance Pay
(checked automatically with a read-only Binance API key, or by the owner from a receipt),
and earnings/withdrawals for sellers.

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

   Open your bot in Telegram and press **Start**. The bot answers only while this program
   runs. To run it 24/7 on a server, see **[DEPLOY.md](DEPLOY.md)**: GitHub can install
   it on your server for you (you add three secrets), or one command does it on the
   server. Either way it runs as a service, with a backup every 6 hours.

On first run the bot sets its own description ("Press /start to start the shop") and the
command list shown by the **Menu** button. The owner and admins see their extra commands
in that list.

## Panels

| Owner (ID 6883493877) | Admin | Customer |
|---|---|---|
| 🛍 Shop | 🛍 Shop | 🛍 Shop |
| 📦 My Products | 📦 My Products (own products) | 💳 Add Balance |
| ➕ Add Product | ➕ Add Product | 🧾 My Orders |
| 🏷 Add Label | 📊 Earnings (own) | 🆔 My ID |
| 📊 Earnings (everyone) | 💳 Add Balance | |
| 💳 Add Balance | 🆔 My ID | |
| 🆔 My ID | | |

`/start` greets with **Welcome Owner**, **Welcome Admin**, or **Welcome _first name_**, and
shows the balance. When deposits or withdrawals are waiting for the owner, the owner panel
also shows a **📥 Pending requests** button.

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
- **🚫 Disable** a label to hide it, and every label and product inside it, from
  customers (nothing is deleted; stock can still be added). **✅ Enable** shows it
  again, except labels inside it that you disabled on their own. Disabled labels are
  marked 🚫 for you.
- Products without a price are hidden from customers.

### 2. Adding products (➕ Add Product)

Open the labels down to the product, then **send one message per product** (text, photo,
file, video…). Each message is delivered to the buyer exactly as you sent it. Press the
green **Done** to save them all (or red **Cancel**). Every product gets a unique
**product ID**, e.g. `K7Q2M9XA`.

- Made a typo? **Edit the message before pressing Done** and the edited version is saved
  instead. After Done, an edit no longer changes the product: remove it in 📦 My Products
  and send the corrected one.
- Captions on photos and files can be at most 1024 characters (Telegram's limit for bots);
  send longer text as its own message.
- After Done, everyone who uses the bot (not the owner, banned users, or whoever added
  the stock) gets a **🆕 New in the shop** message with the product, its price (sellers
  see theirs) and a button to buy it. Stock added within about a minute goes out as one
  message. Nothing is sent while the shop is closed or for a disabled product.

### 3. My Products (📦)

Browse like the shop; a product shows the IDs of its products in stock. Tap an ID to see
what's inside, then:

- **🗑 Remove**: deletes it from the shop.
- **📤 Pull**: takes it out of the shop and sends it to you as if you bought it.

The owner sees and manages everyone's products; an admin only their own.
**📥 Download sales analysis** sends a spreadsheet (CSV) with sold / left / revenue per
product and per seller.

**`/pull`** takes many at once: choose one of your products and how many (1, 5, 10, 50,
all, or any number up to 200 at a time). They're **picked at random** from your stock,
taken out of the shop and sent to you. Admins pull their own; the owner pulls his own
with `/pull`, or an admin's with `/pull ID` (the admin is told how many). A product that
can't be sent goes back in stock, and you're told which; one Telegram didn't answer for (it
may have arrived) is tried once more and otherwise stays pulled, with a button to get it
again, so it's never sold to someone else as well.

### 4. Buying

Customers open 🛍 Shop, pick a product and a quantity, and confirm. The price comes off
their balance and the products are sent right away. The money goes to the **earnings** of
whoever added each product (owner or admin).

**Fair between sellers:** when several sellers have the same product in stock, every sale
is drawn at random between them, all with the **same chance**, however many each one has.
With Adam at 100, Bella at 1 and the owner at 5, each sale is a third each; once Bella's
one is sold it's half and half between Adam and the owner, until someone adds stock. An
order of several is drawn one product at a time (each seller's oldest one goes).

- The buyer always pays the total shown on the **Confirm** screen. If the price changes
  before they press Confirm, nothing is charged and they're shown the new total to confirm.
- Big orders are sent at about one message per second (Telegram's limit per chat) while
  the bot keeps serving everyone else. Customers can get an order again any time from
  🧾 **My Orders**.

### 5. Adding balance (💳 Add Balance)

Pick 10 / 20 / 30 / 50 / 100 $ or **Custom** (at least 1$, cents allowed, e.g. 12.50) and send the money with
Binance Pay to Binance ID **850940840**. Then:

- **With a Binance API key** (automatic, see DEPLOY.md, *Automatic deposits with Binance
  Pay*): send **exactly** that much USDT and send the payment's **Order ID** to the bot.
  When Binance shows a payment with that Order ID, coin and amount, the balance is added at
  once. If not, the owner gets a ⚠️ message and the bot asks for the Order ID again; if the
  second try fails too, the deposit goes to the owner to **Approve** / **Reject**. An Order
  ID can only ever be used once: sent again (by anyone, also while a request with it waits),
  the bot says it was already used and that deposit is cancelled, with no second try (the
  owner is told when someone else sent it); only a payment from a request of theirs the
  owner rejected goes to the owner again, as nobody got it. A deposit also goes to the owner
  when Binance can't be asked (the key is refused, or Binance still can't be reached when the
  customer checks again a minute later), or when the owner may have decided on that payment
  by hand already.
- **Without a key:** send a **photo of the receipt** and press **Confirm**. The owner gets
  the receipt with **Approve** / **Reject** buttons; on approval the balance is added.

If a request ever doesn't reach the owner (or the message gets deleted), `/pending` sends
every waiting request again with its buttons.

**No payment is ever added twice.** Every way money is added for a Binance payment checks
both of its IDs (the Order ID the payer sees and Binance's transaction ID):

- **By hand** (the customer sent the money and wrote to the owner): `/addbal ID AMOUNT
  ORDER_ID` adds the balance and registers the payment in one go, or `/regbin ORDER_ID`
  (optionally `/regbin ORDER_ID USER_ID`) registers it afterwards. With a Binance key the
  bot asks Binance first and shows what it has (amount, coin, time, payer) with
  **✅ Add** / **✅ Register**: nothing is recorded until the owner confirms. If the payment
  may have been added already (balance added by hand since it was paid, a request you
  decided that had it, or it was paid before automatic checking started), the screen says
  why and offers only **⚠️ Add anyway**; if anything new turns up before you press it, send
  the Order ID again. If Binance can't be asked or doesn't list the ID, or the payment was
  added already, nothing is added (while Binance refuses the key, add the balance without
  the Order ID: **⏭ Skip**). Without a key only an Order ID (digits) is taken. Registering
  a payment that waits with that customer's own request (the only one it holds) closes the
  request and tells them; with anyone else's request, decide that first.
- **Balance added by hand** (`/addbal`, with or without an Order ID, **⏭ Skip**, a
  `/modifybal` raise) may have been any payment made before it, so it holds back every
  payment made before it, for everyone: those come to the owner with a ⚠️ line instead of
  being added, the customer's own listed first.
- **Requests:** approving one adds every payment it shows (the request says so when it
  shows several). A request whose payment was added already, is kept for another waiting
  request, or is known only by a transaction ID Binance didn't confirm can't be approved
  (it says why in a ⛔ line); reject it and, if the customer paid with another payment,
  add that one by hand with its Order ID. When the payer of a payment someone else claimed
  in a request you rejected sends it, they're told it was used and you get the
  `/addbal` that adds it for them.
- **Receipts** (deposits without a key) list what was added for that customer, or for a
  Binance payment of the same amount, in the week before: then only **⚠️ Approve anyway**
  approves, so an old receipt sent again isn't added twice by mistake.

**Sellers** get **10% off** every product. Anyone whose total deposits reach **1000$**
becomes a seller automatically, or the owner can use `/seller ID`.

### 6. Earnings and withdrawals

📊 **Earnings** shows the owner's earnings, each admin's earnings, the total earnings and
the total purchases. Admins see their own. `/withdraw` asks for an amount and a Binance ID;
the amount leaves your earnings straight away and the owner gets **Approve** / **Reject**
buttons (if rejected, the money returns to the admin's earnings). The owner's own
withdrawals are recorded immediately. An admin who is removed keeps the money they
earned: they still see 📊 Earnings and can use `/withdraw`.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Open the menu |
| `/help` | everyone | List the commands for your panel |
| `/id` | everyone | Show your Telegram ID (also the 🆔 My ID button). The owner can send `/id @username` to get someone's ID (they must have used the bot) |
| `/cancel` | everyone | Cancel what you're doing |
| `/withdraw` | owner, admins | Withdraw earnings to Binance |
| `/pull` | owner, admins | Take products out of your stock, picked at random, as many as you choose (owner: `/pull ID` for an admin's) |
| `/addbal ID AMOUNT` | owner | Add balance to a user (counts as a deposit). `/addbal ID AMOUNT ORDER_ID` also registers that Binance Pay payment, so it can't be added again. Send `/addbal` alone and the bot asks for the ID, the amount and then (with automatic deposits) the Order ID, which is optional: **⏭ Skip** adds it without one |
| `/regbin ORDER_ID [USER_ID]` | owner | Register a Binance Pay payment you added by hand: it can't be added again |
| `/modifybal ID` | owner | Show a user's balance and set a new one |
| `/addadmin ID` / `/removeadmin ID` | owner | Give / remove the admin panel |
| `/seller ID` / `/removeseller ID` | owner | Give / remove the 10% seller discount |
| `/ban ID` / `/unban ID` | owner | Block / unblock a user from the bot |
| `/close` / `/open` | owner | Close / open the shop for customers |
| `/status` | owner | All users: ID, name, balance, total deposits (+ CSV file) |
| `/pending` | owner | Show the deposits and withdrawals waiting for your answer again |
| `/announce` | owner | Send a message to every user (not you or banned users): `/announce your text`, or `/announce` and then any message (photo, file, video…). You see it first and confirm with **✅ Send** |

Commands that need a user also work without arguments: e.g. send `/ban` and the bot asks
for the ID.

**Edited messages are ignored**: editing a message you already sent (a command, an amount,
an ID) never runs it again. Send a new message instead. The one exception is a product
message in ➕ Add Product before you press Done (see above).

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Token from @BotFather (required) |
| `OWNER_ID` | `6883493877` | Telegram ID of the owner |
| `BINANCE_PAY_ID` | `850940840` | Where customers send deposits |
| `DB_PATH` | `data/shop.db` | Database file |
| `SELLER_DISCOUNT_PERCENT` | `10` | Seller discount |
| `SELLER_AUTO_THRESHOLD` | `1000` | Total deposits ($) that make someone a seller |
| `SUPPORT_CONTACT` | owner's @username | Who customers are told to contact (`@name`, a link, or `none`) |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | — | A read-only Binance API key of the `BINANCE_PAY_ID` account: deposits are then checked automatically by Order ID |
| `BINANCE_COINS` | `USDT` | Coins added 1:1 in $ by automatic deposits (comma-separated, e.g. `USDT,USDC`) |
| `BINANCE_MAX_AGE_HOURS` | `48` | How far back automatic deposits look for a payment |

Back up regularly: `data/shop.db` holds all products, balances and orders. Use
`python -m shopbot.backup` (safe while the bot runs; `--list` and `--restore FILE` too)
rather than copying the file by hand. On a server set up with `deploy/install.sh` this
happens every 6 hours (the newest 28 are kept).

## Tests

The tests run the real bot against a fake Telegram server, so no token is needed:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
