"""What the assistant knows about the app itself: every screen, its tabs, buttons and the steps to do a job.

The assistant answers "how do I..." questions from this text, not from memory. Each entry is a screen of the
desktop app. `guide_for(question)` picks the few entries that fit the question so the prompt stays short.
Keep the names here identical to the words on screen (sidebar, tabs, buttons). When a screen changes, change its entry.
"""

import re

# (screen name as in the sidebar, words that point to it, the guide text)
GUIDE: list[tuple[str, str, str]] = [
    (
        "Overview of the app",
        "app menu sidebar screens modules where find overview start login offline sync shortcut ctrl",
        """Gropto Retail ERP is a desktop app for grocery and retail stores. The left menu has groups:
SALES (Billing, Past Bills, Returns, Online Orders), MONEY (Daily Cash, Finance, Offers & Rewards),
INVENTORY (Products, Check Stock, Warehouse, Stock Transfers, Stock Counting, Stock Control, Import Data),
BUYING (Buying & Vendors, Purchase Entry), INSIGHTS (Sales, Cash Book, Reports, Alerts & Overview),
SETTINGS (Stores, Bill & Receipt, Roles & Access, Users, Warehouses & Vendors, Approvals, Activity Log,
Tills & Devices, Fraud Alerts, Staff, Sync Status). The Dashboard is the home page. A user only sees the screens
their role allows. Ctrl+B opens or closes the side menu. The top bar shows the sync state (Synced / Pending Sync /
Syncing / Sync Failed) with a Retry now button, the store picker and the user menu. The till works offline: bills
are saved on the computer and sent to the server when the internet is back.""",
    ),
    (
        "Dashboard",
        "dashboard home kpi sales today mtd ytd margin inventory customers insight ai generate",
        """Dashboard: cards for Sales (Today, MTD, YTD, bills, average bill, units, offline vs online), Margin (gross margin,
discount leakage), Stores (top and bottom store), Inventory (stock value, in-stock and out-of-stock lines, near-expiry,
dead stock, excess, ageing), Warehouse (pending GRNs and transfers, pick accuracy) and Customers (active, new, repeat %).
Charts show the daily sales trend and top products. The AI Store Insight Assistant card: press Generate AI Insights to get a
short written summary plus the key numbers and low-stock alerts; Generate again refreshes it; the X closes it. The
sparkle button in the top bar opens the AI chat where you can ask questions about your store or how to use the app.""",
    ),
    (
        "Billing (POS)",
        "billing pos till bill sale scan barcode cart customer cash card upi hold split payment coupon discount print "
        "receipt shortcut f2 f3 f4 f5 f6 f8 loyalty price mrp quantity remove clear counter",
        """Billing (the till). Steps to make a bill:
1. Scan a barcode, or type a product name / SKU in the search box and press Enter or ADD. Each scan adds one unit;
   scanning again increases the quantity. If a product is on sale at more than one price, a picker asks which MRP printed on
   the pack; choose the matching one.
2. Optional: click the customer field (or press F2), type the 10-digit mobile number to attach a customer for loyalty points,
   and a name to print on the bill.
3. Change quantity with the + and - buttons in the row; the bin button removes a line. Del removes the last item.
4. Discounts: automatic offers apply by themselves. Type a coupon in the coupon box (F8) and press Apply. Loyalty points can be
   redeemed with the Redeem box when a customer is attached.
5. Pay: the bottom bar shows Items, Units, Subtotal, Discount, GST and Total payable. Press Cash (F3), Card (F5) or UPI (F6).
   For cash, type the amount received and the change is shown. Split lets you pay part cash, part card/UPI/wallet/gift voucher.
   Hold (F4) parks the bill to serve another customer; Held bills are listed under Parked / Held Bills and can be resumed or discarded.
6. A print dialog shows the bill preview; print it or Skip.
Esc asks to clear the current bill. A manager override dialog appears when a cashier does something that needs approval
(for example a big discount). The counter number identifies the till (device) that made the bill and is printed with the bill number.""",
    ),
    (
        "Past Bills",
        "past bills recent bills history reprint preview invoice search receipt",
        """Past Bills lists earlier bills of the store. Search by bill number or date. Each row has a Preview button that opens the receipt
exactly as it was printed, and you can print it again from there. Use Returns when a customer brings an item back.""",
    ),
    (
        "Returns",
        "return refund exchange credit note damaged scrap vendor saleable bill preview past bills",
        """Returns. Step 1: find the original bill: search by mobile number, customer name, bill number or an item, or press
Preview past bills to browse and pick one. Step 2: choose the items and quantities coming back (not more than were sold),
give a reason (for example damaged packaging or customer request), and choose where the stock goes: Back to Saleable Stock,
Damaged / Scrap, or Return to Vendor. Step 3: confirm. The refund amount follows the price actually charged on the original bill.
Some returns may need a manager's approval.""",
    ),
    (
        "Online Orders",
        "online orders omnichannel delivery rider pick pack dispatch otp reserved allocated delivered cancel app web marketplace fill rate",
        """Online Orders handles orders from the app, web or marketplaces. Create Omnichannel Order: choose the fulfilling store, add products,
enter the customer's 10-digit mobile, optional address and coupon. An order moves through Reserved -> Allocated -> Picking ->
Picked -> Packed -> Dispatched -> Delivered (or Cancelled). Assign a delivery rider (an employee) at dispatch. At delivery enter the
customer's 4-digit OTP. Scan a barcode while picking to verify the item. The top cards show orders this month, fill rate,
cancellations, items not found, average delivery time, pick and pack time and the dispatch SLA.""",
    ),
    (
        "Daily Cash",
        "daily cash shift open close float denomination notes coins drawer cash in out expense petty day close reconciliation",
        """Daily Cash has three tabs. Shifts: start a shift by counting the Opening Float (Step 1: count each note and coin with the
denomination counter), and at the end enter the Closing Cash Count; the Reconciliation Summary shows expected cash against counted cash
and the difference; Confirm Shift Closure saves it. Cash in and out: record money put into or taken from the drawer, with a reason
(for example vendor payment or tea/snacks petty expense). Day close: the store's business-day close with the day's checklist; Confirm
Store Day Close locks the day.""",
    ),
    (
        "Finance",
        "finance expense bank deposit slip budget capex opex payment check accountant journal export p&l profit loss e-invoice irn gst",
        """Finance: Record Store Expense (category such as utilities, rent, maintenance, packaging, amount, notes). Log Daily Bank Cash
Deposit Slip (bank name, slip number, amount) and see the list of recorded slips. Set / Update Store Budget and compare it with actual
spend (Capex / Opex breakdown). Payment Check compares what payment gateways report with the bills. Export for Accountant produces
journal lines for a date range. Consolidated Company P&L (month to date) and a per-store breakdown sorted by net contribution.
E-Invoice Status by Store and the E-Invoice Status Log show each bill's status (Not Applicable, Pending, Submitted, IRN Generated,
Failed, Cancelled).""",
    ),
    (
        "Offers & Rewards",
        "offers coupons promotions discount rule loyalty points tier wallet price change schedule deactivate edit validity create",
        """Offers & Rewards has two parts. Offers & Coupons (Promotions): create an Offer (choose products, discount type and value, start
and end dates, stores) or a Coupon (a code such as WELCOME50 with its discount, limits and validity). Use the toggle to deactivate or
reactivate any offer or coupon, and Edit to change it. Loyalty settings use tabs: Points (earn rate points per rupee, redeem value rupees
per point, minimum balance to redeem, maximum share of the bill that can be paid with points, points expiry in days), Discount Rules,
Tiers (name, minimum points, multiplier), Wallet (credit or adjust a customer's wallet with a reason) and Price Changes (schedule a
price change for a date; it goes through approval). Changes to loyalty rules and prices may wait for Super Admin approval.""",
    ),
    (
        "Products",
        "products catalogue add edit sku barcode mrp price selling purchase gst hsn brand category unit variant old stock other price expand collapse buyers who buys deactivate",
        """Products. Add a product: press Add, fill the name, SKU, barcode, brand, category, unit, MRP, selling price, purchase price,
GST % and HSN, then save. Search by barcode, SKU, HSN, name or brand. The expand button makes the screen full size and Collapse (or Esc) returns.
Prices: the main price is the Current price. If a supplier raises the price while old stock remains, add a second price ("Also on sale at
another price"). The tabs Current price / Old stock show how many units sit at each price; the till then asks which MRP is printed on
the pack. When the old stock is finished, use the stop-selling button on that old price. "Where the stock is" shows stock by store and
warehouse. The buyers button shows which customers bought the product, with date filters. Price changes and switching a product off
can go to approval.""",
    ),
    (
        "Check Stock",
        "check stock lookup batch expiry fefo fifo expiring ledger quantity available",
        """Check Stock: search a product name, SKU or barcode to see its stock by store, the batch and expiry ledger (FEFO/FIFO order), and the
list of items expiring in the next 90 days or already expired.""",
    ),
    (
        "Warehouse",
        "warehouse wms pick pack dispatch put-away putaway store request task grn receive",
        """Warehouse (WMS): choose a warehouse. Warehouse stock shows what is in it. Pending Put-away Tasks: put received goods on shelves.
Pick & Pack: build a pick list from a store request (scan or type products), Confirm Picked Quantity, pack, then Dispatch Packed Tasks
to the store. A pick cannot finish if the warehouse does not have enough stock; receive stock (GRN or transfer) first.""",
    ),
    (
        "Stock Transfers",
        "transfer stock between stores warehouse send receive tr slip draft mismatch dispatch csv bulk",
        """Stock Transfers moves stock between places. New Transfer: choose From and To, add items by scanning or typing a barcode, SKU, name or
HSN (or Bulk Select Stock, or upload a CSV; Download Sample CSV gives the format). Save as a draft (nothing leaves the sender) or send it. The
receiver opens the transfer and enters the good units that arrived and any arrived but not sellable. Statuses: Saved draft, On the way,
Part received, Received, Quantity mismatch, Mismatch closed, Cancelled. Filters: All, Drafts, Needs checking, date range, place.
"Sent but not received" and "More arrived than was sent" list problems to resolve. A transfer slip can be printed.""",
    ),
    (
        "Stock Counting",
        "stock count counting physical audit variance recount cycle count",
        """Stock Counting: press start a stock count, choose products (search name, SKU or barcode) or the whole store, then enter the counted
quantities and press Save Counted Quantities. Differences from the system stock are shown and the stock can be adjusted to match.""",
    ),
    (
        "Stock Control",
        "stock control reorder point low stock adjust adjustment slow dead expiry sell through shrinkage ageing overstock demand forecast ranking abc",
        """Stock Control has tabs: Low Stock Alerts (set a Product Reorder Point per product), Adjust Stock (Manual Stock Adjustment with a
reason; large adjustments need approval), Item Ranking (top seller, regular, slow seller, daily staple, ups and downs, occasional),
Slow Stock, Expiry (value at risk, forecast for the next 7 to 90 days), Sales Speed (sell-through over 30 days), Stock Loss (shrinkage over
90 days), Move Stock Ideas (transfer recommendations between stores), Stock Age (ageing by receipt date) and Overstock (stock above demand).""",
    ),
    (
        "Import Data",
        "import csv excel upload bulk products opening stock customers vendors template other prices batches preview add update problem",
        """Import Data: pick what to import: Product Master Catalog, Opening Stock Initialization, Other Prices (batches), Customers (by mobile
number) or Vendors (with GST and contact details). Download the template, fill it, and upload it. A preview sorts rows into Add, Update,
No change and Problem; fix the problem rows, then confirm the import. Nothing is saved until you confirm.""",
    ),
    (
        "Buying & Vendors",
        "buying procurement purchase order po grn goods receipt vendor invoice payables three way match rfq quote supplier fill rate",
        """Buying & Vendors has tabs: Purchase orders (create a New Purchase Order: pick the vendor, add products with ordered quantity and cost; a
purchase order may need approval), Goods received (record a Goods Receipt Note against a PO with received quantities), Vendor invoices (Record Vendor
Invoice with a 3-way match against the PO and GRN), Payables (what we owe each vendor) and Vendors (the vendor list: add, edit, GSTIN, PAN, contact).
The top shows vendor fill rate, lead time and rejection rate and the purchase order pipeline.""",
    ),
    (
        "Purchase Entry",
        "purchase entry bill vendor invoice stock in cost update inward new vendor view bill",
        """Purchase Entry records a vendor's bill and puts the stock in. 1. Scan or type products and press Enter to add the first match.
2. Check quantities and cost (and MRP, GST) per line. 3. Choose the vendor (or add a New vendor) and enter the vendor's invoice number,
then record. Past entries can be opened with View bill and printed. Recording raises stock and updates the cost price.""",
    ),
    (
        "Sales (Insights)",
        "sales summary day by day counter store totals insights",
        """Sales (under Insights) shows sales Day by day, By counter (each till) and as Store totals for a date range.""",
    ),
    (
        "Cash Book",
        "cash book where cash came went in out",
        """Cash Book lists where the cash came from (put in) and where it went (taken out) for a period, to match the drawer.""",
    ),
    (
        "Reports",
        "reports export download csv sales details hourly stock movement purchase prices mismatch transfers profit customer groups loyalty history store performance checklist",
        """Reports: choose a report from the tabs: Sales Details, Hourly Sales, Current Stock, Stock Movements, Purchase Prices, Goods Receipt
Mismatches, Pending Transfers, Profit by Store, Profit Change Explained, Customer Groups, Loyalty Points History, Store Performance and
Daily Checklist Report. Pick the dates and store, then view or export.""",
    ),
    (
        "Alerts & Overview",
        "alerts overview enterprise head office all stores performance problems suspicious bills sync failures mismatch tills health channel",
        """Alerts & Overview is the head-office view across stores: pending approvals, open fraud alerts, unresolved sync failures, transfer
discrepancies, suspicious bills, product sales and profit (30 days), stock mismatch check, tills health, online delivery speed, sales by channel and
store performance. Each card has a View button that jumps to the screen with the details.""",
    ),
    (
        "Stores",
        "stores new store create edit gst footfall franchise location company state target",
        """Stores (Settings): add a store with its code (for example AGR001), name, region, GSTIN, state, size and targets; Edit Store changes the details;
the footfall box logs the day's walk-in count for the conversion rate. A new store or a change to store details may need Super Admin approval.""",
    ),
    (
        "Bill & Receipt",
        "bill receipt settings shop name address gst footer message thermal print layout header",
        """Bill & Receipt: set the shop details printed on the bill (large name, legal name, address, GSTIN, phone), the title (for example TAX
INVOICE/RECEIPT) and the message at the bottom of the bill (for example Thanks for your visit). Settings apply to every store by default, or choose one store.
Undo changes reverts unsaved edits.""",
    ),
    (
        "Roles & Access",
        "roles access permissions rbac matrix grant custom role approval matrix user access",
        """Roles & Access (admin): Roles (create a custom role with no spaces in its name, for example night_supervisor, or delete one), Matrix (tick the
permissions each role has; Grant All gives every permission; each change asks for a reason that is recorded in the audit trail), Approval Matrix
(which actions need approval and from whom) and User Access (search a person and give or remove permissions for just that person, with a reason).""",
    ),
    (
        "Users",
        "users staff login account create onboard password reset role store assign cashier manager admin deactivate",
        """Users: Onboard New User Account: enter name, email, phone (+91...), password (minimum 6 characters), role (CEO / Head Office, COO,
Finance Head, Purchase Head, Regional Manager, Store Manager, Cashier, Warehouse User, System Admin, Admin) and the store(s). A Cashier or Store
Manager belongs to one store. Edit User changes the role or stores; Reset Credentials sets a new password. A new login created by an Admin may need
Super Admin approval.""",
    ),
    (
        "Warehouses & Vendors",
        "master data warehouse create vendor add supplier code",
        """Warehouses & Vendors has two tabs. Warehouses: add a warehouse with a code (for example WH-DEL-01), name and city. Vendors: the supplier list with
GSTIN, PAN, CIN, address and contact; add or edit a vendor and search by name, GSTIN, PAN, city or phone.""",
    ),
    (
        "Approvals",
        "approvals approve reject pending maker checker request price change big discount adjustment loyalty switch off purchase order",
        """Approvals: some actions by an Admin or a manager wait here for the Super Admin: price changes, extra prices for a product, scheduled price
changes, big discounts, large stock adjustments, loyalty rule changes, switching a product off, new stores and store changes, new staff logins, access
changes and purchase orders. Open an item to see what changes (old value and new value), add an optional note, and Approve or Reject. The Super Admin's
own changes apply immediately.""",
    ),
    (
        "Activity Log",
        "activity log audit trail who changed history entity user action export",
        """Activity Log (audit trail): every important change with who, when, what and the old and new values. Filter by action, store, entity type
(for example product) or user, and export it.""",
    ),
    (
        "Tills & Devices",
        "tills devices counter register device broadcast configuration offline limit",
        """Tills & Devices lists each till (device) with its counter number, last seen and status. Device Configuration changes its settings, and
Broadcast to All Devices sends one configuration to every till.""",
    ),
    (
        "Fraud Alerts",
        "fraud alerts suspicious void discount cashier security",
        """Fraud Alerts lists suspicious activity (for example unusual discounts, voids or returns by a cashier). Open an alert, review it and resolve it
with a note. When there are none it shows No Open Security Alerts.""",
    ),
    (
        "Staff",
        "staff hr employee attendance shift schedule payroll incentive penalty transfer present late absent leave department",
        """Staff (HR): Employee Directory & Onboarding (add an employee with designation, department, warehouse, reporting manager and a linked login),
Attendance (Present, Late, Absent, Leave, Weekly Off), Shift Schedule Configurator (create shifts such as Morning or Evening), Incentives and
Penalties, Inter-Store Staff Transfer and the Monthly Payroll Input Breakdown export.""",
    ),
    (
        "Sync Status",
        "sync status failed retry pending offline outbox queue",
        """Sync Status shows what the till has sent to the server and what is waiting. Pending means saved on this computer, not yet sent. Failed items
show the reason; press Retry now in the top bar to try again. Bills made offline are never lost; they are sent when the internet returns.""",
    ),
    (
        "Customers (CRM)",
        "customers crm rfm champion churn segment audience campaign cohort clv complaint ticket",
        """Customers: RFM customer matrix (Champion, Recent, Regular, At Risk, Churned), Customer 360 profile (orders, loyalty, coupons, refunds,
complaints), Audience Builder (save a group of customers), campaigns, CLV segments, cohort retention and churn-risk customers.""",
    ),
]

_WORD = re.compile(r"[a-z0-9&]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def guide_for(question: str, limit: int = 4) -> str:
    """The guide entries that best fit the question, plus the overview, as plain text for the prompt."""
    q = _words(question)
    scored = []
    for name, keys, body in GUIDE:
        score = len(q & _words(keys)) * 2 + len(q & _words(name))
        scored.append((score, name, body))
    scored.sort(key=lambda t: -t[0])
    picked = [t for t in scored if t[0] > 0][:limit]
    overview = next(t for t in scored if t[1] == "Overview of the app")
    if overview not in picked:
        picked.append(overview)
    return "\n\n".join(f"[{name}]\n{body}" for _, name, body in picked)
