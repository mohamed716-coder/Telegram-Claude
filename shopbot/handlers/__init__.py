"""Wire every handler into the Application."""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import common, labels, money, owner, shop, stock
from . import broadcast  # after stock: it uses stock's checks

COMMANDS = [
    ("start", common.cmd_start),
    ("help", common.cmd_help),
    ("cancel", common.cmd_cancel),
    ("withdraw", money.cmd_withdraw),
    ("addbal", owner.cmd_addbal),
    ("modifybal", owner.cmd_modifybal),
    ("addadmin", owner.cmd_addadmin),
    ("removeadmin", owner.cmd_removeadmin),
    ("seller", owner.cmd_seller),
    ("removeseller", owner.cmd_removeseller),
    ("ban", owner.cmd_ban),
    ("unban", owner.cmd_unban),
    ("close", owner.cmd_close),
    ("open", owner.cmd_open),
    ("status", owner.cmd_status),
    ("pending", money.cmd_pending),
    ("announce", broadcast.cmd_announce),
]

# (regex on callback_data, handler). Callback data formats:
#   sh:<label>:<page>  shop       sb:<label>:<qty>  quantity      sq:<label>  other qty
#   sc:<label>:<qty>:<nonce>  confirm purchase      mo:<page> / mov:<order>  my orders
#   lb/ln/lp/lr/ld/ldy/lx  labels (lx = disable/enable)   ap/apd/apc  add product
#   mp/mi/mv/mr/mry/mu/ma  my products               dp/da/dc/dsend/dok/dno  deposits
#   ea/ew/wall/wok/wno  earnings & withdrawals      pq  pending requests (owner)
#   an:<nonce> / anc[:<nonce>] / ans:<job>  send / cancel / stop an announcement (owner)
#   menu/cancel/noop  general
CALLBACKS = [
    (r"^menu$", common.cb_menu),
    (r"^cancel$", common.cb_cancel),
    (r"^noop$", common.cb_noop),
    (r"^sh:", shop.cb_shop),
    (r"^sb:", shop.cb_quantity),
    (r"^sq:", shop.cb_custom_quantity),
    (r"^sc:", shop.cb_confirm),
    (r"^mo:", shop.cb_orders),
    (r"^mov:", shop.cb_order_view),
    (r"^lb:", labels.cb_labels),
    (r"^ln:", labels.cb_new_label),
    (r"^lp:", labels.cb_set_price),
    (r"^lr:", labels.cb_rename),
    (r"^ld:", labels.cb_delete),
    (r"^ldy:", labels.cb_delete_yes),
    (r"^lx:", labels.cb_disable),
    (r"^ap:", stock.cb_add_browse),
    (r"^apd$", stock.cb_add_done),
    (r"^apc$", stock.cb_add_cancel),
    (r"^mp:", stock.cb_mine),
    (r"^mi:", stock.cb_items),
    (r"^mv:", stock.cb_item_view),
    (r"^mr:", stock.cb_item_remove),
    (r"^mry:", stock.cb_item_remove_yes),
    (r"^mu:", stock.cb_item_pull),
    (r"^ma$", stock.cb_analysis),
    (r"^dp$", money.cb_deposit_menu),
    (r"^da:", money.cb_deposit_amount),
    (r"^dc$", money.cb_deposit_custom),
    (r"^dsend$", money.cb_deposit_send),
    (r"^(dok|dno):", money.cb_deposit_decide),
    (r"^ea$", money.cb_earnings),
    (r"^ew$", money.cb_withdraw),
    (r"^wall$", money.cb_withdraw_all),
    (r"^(wok|wno):", money.cb_withdraw_decide),
    (r"^pq$", money.cb_pending),
    (r"^an:", broadcast.cb_announce_send),
    (r"^anc(:|$)", broadcast.cb_announce_cancel),
    (r"^ans:", broadcast.cb_announce_stop),
]


def register_handlers(app: Application) -> None:
    # Only new messages are input. python-telegram-bot's filters also match edited
    # messages, which would run an edited command again (e.g. /addbal twice).
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    # Group -1 runs first for every update: user tracking, bans, ending flows.
    app.add_handler(TypeHandler(Update, common.pre_handler), group=-1)
    for name, callback in COMMANDS:
        app.add_handler(CommandHandler(name, callback, filters=private))
    for pattern, callback in CALLBACKS:
        app.add_handler(CallbackQueryHandler(callback, pattern=pattern))
    app.add_handler(CallbackQueryHandler(common.cb_unknown))
    app.add_handler(MessageHandler(private & filters.COMMAND, common.on_unknown_command))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, common.on_message))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.UpdateType.EDITED_MESSAGE, common.on_edited_message
    ))
    app.add_error_handler(common.on_error)
