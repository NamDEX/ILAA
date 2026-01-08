import pandas as pd
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta
import os
import sys

### USER CONFIGURATION ###
# File Paths
INPUT_FILE = r"C:\Projects Python\Crypto DEC25\Output\master_crypto_ledger.csv"
OUTPUT_DIR = r"C:\Projects Python\Crypto DEC25\Output\Tax Files"

# UK Tax Year Dates (2024/25)
TAX_YEAR_START = date(2024, 4, 6)
TAX_YEAR_END = date(2025, 4, 5)

# User Tax Profile (Higher Rate Payer)
# User has full allowances available for Crypto (ISAs cover other assets)
INCOME_TAX_ALLOWANCE = Decimal("1000.00")  # Trading Allowance
INCOME_TAX_RATE = Decimal("0.40")          # 40% Tax Rate
CGT_ALLOWANCE = Decimal("3000.00")         # £3k Capital Gains Allowance

# CGT Rates (Higher Rate Payer Only)
RATE_CHANGE_DATE = date(2024, 10, 30)
CGT_RATE_PRE_OCT = Decimal("0.20")         # 20% (Old Higher Rate)
CGT_RATE_POST_OCT = Decimal("0.24")        # 24% (New Higher Rate)

class Pool:
    def __init__(self, coin):
        self.coin = coin
        self.total_qty = Decimal("0")
        self.total_cost_gbp = Decimal("0")

    def add(self, qty, cost):
        self.total_qty += qty
        self.total_cost_gbp += cost

    def remove(self, qty):
        if self.total_qty == 0:
            return Decimal("0")

        # Calculate average cost
        # Precision important here? Standardize to 9 decimals for calculation?
        # Using context precision might be better, but let's just do standard division
        avg_cost = self.total_cost_gbp / self.total_qty

        cost_removed = qty * avg_cost

        self.total_qty -= qty
        self.total_cost_gbp -= cost_removed

        # Handle tiny dust errors if qty goes to 0
        if self.total_qty < Decimal("0.000000001"):
            self.total_qty = Decimal("0")
            self.total_cost_gbp = Decimal("0")

        return cost_removed

class Transaction:
    def __init__(self, id, time_utc, txn_type, coin, qty, total_gbp):
        self.id = id
        self.time = time_utc
        self.type = txn_type
        self.coin = coin

        # CRITICAL SIGNAGE LOGIC: Convert to Absolute Values
        self.initial_qty = abs(Decimal(str(qty)))
        self.total_gbp_val = abs(Decimal(str(total_gbp)))

        self.qty_remaining = self.initial_qty

def load_data(filepath):
    """
    Reads CSV, parses dates, sorts chronologically, and returns list of Transaction objects.
    """
    if not os.path.exists(filepath):
        print(f"Error: Input file not found at {filepath}")
        return []

    df = pd.read_csv(filepath)

    # Parse Dates
    # Format might be ISO or Excel style depending on previous output.
    # Previous script outputted standard ISO strings for CSV.
    df['TIME_UTC'] = pd.to_datetime(df['TIME_UTC'], utc=True)

    # Sort Chronologically
    df = df.sort_values(by='TIME_UTC', ascending=True)

    transactions = []
    for idx, row in df.iterrows():
        # Create Transaction Object
        # ID is strictly internal 0, 1, 2...
        t = Transaction(
            id=len(transactions), # sequential ID
            time_utc=row['TIME_UTC'],
            txn_type=row['TXN_TYPE'],
            coin=row['COIN'],
            qty=row['QTY'],
            total_gbp=row['TOTAL_GBP']
        )
        transactions.append(t)

    return transactions

def main():
    print("Starting UK Crypto Tax Engine 2024/25...")

    # 1. Load Data
    transactions = load_data(INPUT_FILE)
    if not transactions:
        sys.exit(1)

    print(f"Loaded {len(transactions)} transactions.")

    # 2. Matching Engine
    pools = {} # Map coin -> Pool()
    audit_log = [] # List of dicts

    # Iterate through every transaction
    for i, txn in enumerate(transactions):

        # Helper to ensure pool exists
        if txn.coin not in pools:
            pools[txn.coin] = Pool(txn.coin)

        pool = pools[txn.coin]

        if txn.type in ['BUY', 'INCOME']:
            # Check qty_remaining (B&B might have consumed it)
            if txn.qty_remaining > 0:
                # Add proportional cost to pool
                # Cost Basis for pool add = (remaining / initial) * total_gbp_val
                # Note: INCOME acts as Buy with cost basis = value at receipt

                # Careful with division by zero if initial is somehow 0 (shouldn't be for valid txns)
                if txn.initial_qty == 0:
                    cost_to_add = Decimal("0")
                else:
                    cost_to_add = (txn.qty_remaining / txn.initial_qty) * txn.total_gbp_val

                pool.add(txn.qty_remaining, cost_to_add)
                # We don't log a 'Match' for a Buy adding to pool, strictly speaking,
                # but we could log it as 'POOL_ENTRY'. The prompt asks for Audit Log for Sells mostly?
                # "Only process Audit Log entries where Sell_Date is between..."
                # So we only really care about logging matches for Sells.

        elif txn.type == 'SELL':
            qty_to_sell = txn.initial_qty
            qty_sold_so_far = Decimal("0")

            # While loop to satisfy sell
            # Use a safety break to prevent infinite loops on precision issues
            loop_count = 0
            while qty_sold_so_far < qty_to_sell and loop_count < 1000:
                loop_count += 1

                chunk_qty = qty_to_sell - qty_sold_so_far
                match_type = ""
                matched_txn = None
                cost_basis = Decimal("0")

                # Priority 1: Same Day Rule
                # Look for BUY/INCOME on same date with qty_remaining > 0
                # "Same date" means same calendar day. Timestamps are UTC.
                # HMRC usually uses transaction date.

                sell_date = txn.time.date()

                # Scan all transactions? Or just optimization?
                # Since we have the list, we can scan.
                # Optimization: Same day must be... roughly nearby in the sorted list?
                # But list is sorted by Time. Same date could be earlier or later in the list if times differ.
                # Wait, "Same Day" rule applies to shares acquired on the *same day*.
                # If sorted by time, same day buys could be before or after this sell (if time is tracked).
                # Actually, if we process chronologically, Buys on same day *before* this sell have been processed (added to pool or consumed).
                # Wait. "Same Day" rule says: "all shares of the same class acquired by you on the same day as the disposal".
                # This includes shares acquired *after* the disposal on the same day.
                # So we need to look at the whole day.

                # Strategy: Search for candidate
                candidate = None
                for other in transactions:
                    if other.coin == txn.coin and other.type in ['BUY', 'INCOME'] and other.qty_remaining > 0:
                        if other.time.date() == sell_date:
                            candidate = other
                            break # Match first found?

                if candidate:
                    match_type = "SAME_DAY"
                    matched_txn = candidate
                    # Determine chunk size
                    matched_amt = min(chunk_qty, candidate.qty_remaining)

                    # Cost basis
                    # Proportional cost of the matched txn
                    cost_basis = (matched_amt / candidate.initial_qty) * candidate.total_gbp_val

                    # Update candidate
                    candidate.qty_remaining -= matched_amt

                    # If the candidate was already processed (i.e. index < i),
                    # we must retroactively remove it from the pool?
                    # Ah, this is the tricky part of "Same Day" if iterating chronologically.
                    # If `candidate` is chronologically BEFORE `txn`, we already added it to the Pool in step "BUY/INCOME".
                    # We shouldn't have added it to the pool if it was destined for Same Day match.
                    # OR, we take it OUT of the pool now.

                    # Let's check logic:
                    # If candidate.index < current.index: It was added to pool.
                    # We must remove that specific amount from the pool to use it for Same Day.
                    # This implies "un-pooling".
                    # Is it better to NOT add to pool until we are sure? No, that's complex.
                    # Better: If we match a past transaction that is in the pool, we remove from pool.

                    if transactions.index(candidate) < i:
                        # It is in the pool. Remove specifically.
                        # Note: `pool.remove` usually uses avg cost. But Same Day uses ACTUAL cost.
                        # So we must manually adjust pool totals without triggering avg cost logic.
                        pool.total_qty -= matched_amt
                        pool.total_cost_gbp -= cost_basis

                        # Verify pool doesn't go negative (it shouldn't if logic holds)

                    # Update Sold
                    qty_sold_so_far += matched_amt

                    # Log
                    audit_log.append({
                        'TIME_UTC': txn.time,
                        'COIN': txn.coin,
                        'ORIGINAL_SELL_ID': txn.id,
                        'MATCH_TYPE': match_type,
                        'MATCHED_WITH_ID': matched_txn.id,
                        'QTY_SOLD_CHUNK': matched_amt,
                        'PROCEEDS_GBP': (matched_amt / txn.initial_qty) * txn.total_gbp_val,
                        'COST_BASIS_GBP': cost_basis,
                        'GAIN_GBP': ((matched_amt / txn.initial_qty) * txn.total_gbp_val) - cost_basis,
                        'SELL_DATE': sell_date
                    })
                    continue # Loop again for next chunk

                # Priority 2: Bed & Breakfast (Future Buy within 30 days)
                # Look for BUY/INCOME > sell_date and <= sell_date + 30
                # Must be chronologically AFTER (index > i)

                candidate = None
                # Limit search to future
                for other in transactions[i+1:]:
                    if other.coin == txn.coin and other.type in ['BUY', 'INCOME'] and other.qty_remaining > 0:
                        days_diff = (other.time.date() - sell_date).days
                        if 0 < days_diff <= 30: # Strictly after (days_diff > 0 is implied by index? No, could be same day later time. Same day handled above.)
                            # "Same Day" covers all on same date. So days_diff must be >= 1 for B&B?
                            # Yes, B&B is "subsequent 30 days".
                            if days_diff >= 1:
                                candidate = other
                                break # Match earliest

                if candidate:
                    match_type = "BED_AND_BREAKFAST"
                    matched_txn = candidate
                    matched_amt = min(chunk_qty, candidate.qty_remaining)

                    cost_basis = (matched_amt / candidate.initial_qty) * candidate.total_gbp_val

                    # Deduct from future buy immediately
                    candidate.qty_remaining -= matched_amt

                    # Update Sold
                    qty_sold_so_far += matched_amt

                    audit_log.append({
                        'TIME_UTC': txn.time,
                        'COIN': txn.coin,
                        'ORIGINAL_SELL_ID': txn.id,
                        'MATCH_TYPE': match_type,
                        'MATCHED_WITH_ID': matched_txn.id,
                        'QTY_SOLD_CHUNK': matched_amt,
                        'PROCEEDS_GBP': (matched_amt / txn.initial_qty) * txn.total_gbp_val,
                        'COST_BASIS_GBP': cost_basis,
                        'GAIN_GBP': ((matched_amt / txn.initial_qty) * txn.total_gbp_val) - cost_basis,
                        'SELL_DATE': sell_date
                    })
                    continue

                # Priority 3: Section 104 Pool
                # If we get here, no Same Day or B&B.
                match_type = "SECTION_104_POOL"
                matched_amt = chunk_qty # Take remaining needed

                # Remove from pool (AVG COST)
                cost_basis = pool.remove(matched_amt)

                qty_sold_so_far += matched_amt

                audit_log.append({
                    'TIME_UTC': txn.time,
                    'COIN': txn.coin,
                    'ORIGINAL_SELL_ID': txn.id,
                    'MATCH_TYPE': match_type,
                    'MATCHED_WITH_ID': 'POOL',
                    'QTY_SOLD_CHUNK': matched_amt,
                    'PROCEEDS_GBP': (matched_amt / txn.initial_qty) * txn.total_gbp_val,
                    'COST_BASIS_GBP': cost_basis,
                    'GAIN_GBP': ((matched_amt / txn.initial_qty) * txn.total_gbp_val) - cost_basis,
                    'SELL_DATE': sell_date
                })

    # 3. Reporting & Tax Calculation
    print("Calculating Tax...")

    # A. Income Tax
    total_income_val = Decimal("0")
    for txn in transactions:
        if txn.type == 'INCOME':
            # Check if in tax year
            if TAX_YEAR_START <= txn.time.date() <= TAX_YEAR_END:
                total_income_val += txn.total_gbp_val

    taxable_income = max(Decimal("0"), total_income_val - INCOME_TAX_ALLOWANCE)
    income_tax_due = taxable_income * INCOME_TAX_RATE

    # B. CGT
    net_gain_p1 = Decimal("0") # Pre Oct 30
    net_gain_p2 = Decimal("0") # Post Oct 30 (inclusive)

    for entry in audit_log:
        s_date = entry['SELL_DATE']
        if TAX_YEAR_START <= s_date <= TAX_YEAR_END:
            gain = entry['GAIN_GBP']

            if s_date < RATE_CHANGE_DATE:
                net_gain_p1 += gain
                entry['TAX_PERIOD'] = "Period 1"
            else:
                net_gain_p2 += gain
                entry['TAX_PERIOD'] = "Period 2"
        else:
            entry['TAX_PERIOD'] = "OUT_OF_SCOPE"

    # Optimization Logic
    # Deduct Allowance from P2 first (24% rate) then P1 (20% rate)
    # But allowance applies to NET GAINS.
    # If Net Gain P2 > 0: use allowance.
    # If Net Gain P2 < 0: It's a loss. Losses offset gains of same year.
    # We should aggregate total gain first?
    # HMRC: "Allowances are set against gains in the way that gives the best result".
    # Losses must be used first to reduce gains.
    # If P2 has a loss, it reduces P1 gain. If P1 has a loss, it reduces P2 gain.

    # Let's normalize Net Gains first
    taxable_p1 = Decimal("0")
    taxable_p2 = Decimal("0")
    allowance_used_p1 = Decimal("0")
    allowance_used_p2 = Decimal("0")

    total_gain = net_gain_p1 + net_gain_p2

    if total_gain <= 0:
        # Total loss or zero, no tax.
        pass
    elif total_gain <= CGT_ALLOWANCE:
        # Covered by allowance fully.
        # Attribution doesn't matter for tax, but for reporting:
        # Attribute allowance to P2 then P1?
        pass
    else:
        # Taxable amount exists.
        remaining_allowance = CGT_ALLOWANCE

        # 1. Use against P2 (Higher Rate)
        if net_gain_p2 > 0:
            used = min(net_gain_p2, remaining_allowance)
            allowance_used_p2 = used
            taxable_p2 = net_gain_p2 - used
            remaining_allowance -= used

        # 2. Use against P1 (Lower Rate)
        if net_gain_p1 > 0 and remaining_allowance > 0:
            used = min(net_gain_p1, remaining_allowance)
            allowance_used_p1 = used
            taxable_p1 = net_gain_p1 - used
            remaining_allowance -= used

    cgt_due_p1 = taxable_p1 * CGT_RATE_PRE_OCT
    cgt_due_p2 = taxable_p2 * CGT_RATE_POST_OCT
    total_cgt_due = cgt_due_p1 + cgt_due_p2

    # 4. Output Generation
    if not os.path.exists(OUTPUT_DIR):
        try:
            os.makedirs(OUTPUT_DIR)
        except:
            pass

    # File 1: Summary
    with open(os.path.join(OUTPUT_DIR, "Tax_Summary_2024_25.txt"), "w") as f:
        f.write("UK CRYPTO TAX SUMMARY 2024/25\n")
        f.write("=============================\n\n")

        f.write("INCOME TAX\n")
        f.write("----------\n")
        f.write(f"Total Crypto Income: £{total_income_val:,.2f}\n")
        f.write(f"Trading Allowance:   £{INCOME_TAX_ALLOWANCE:,.2f}\n")
        f.write(f"Taxable Income:      £{taxable_income:,.2f}\n")
        f.write(f"Tax Rate:            {INCOME_TAX_RATE*100:.0f}%\n")
        f.write(f"Est. Income Tax Due: £{income_tax_due:,.2f}\n\n")

        f.write("CAPITAL GAINS TAX (CGT)\n")
        f.write("-----------------------\n")
        f.write(f"Net Gain Period 1 (< 30 Oct): £{net_gain_p1:,.2f}\n")
        f.write(f"Net Gain Period 2 (>=30 Oct): £{net_gain_p2:,.2f}\n")
        f.write(f"Total Net Gain:               £{total_gain:,.2f}\n")
        f.write(f"CGT Allowance:                £{CGT_ALLOWANCE:,.2f}\n")
        f.write(f"Allowance Used P2 (24%):      £{allowance_used_p2:,.2f}\n")
        f.write(f"Allowance Used P1 (20%):      £{allowance_used_p1:,.2f}\n")
        f.write(f"Taxable Gain P1:              £{taxable_p1:,.2f}\n")
        f.write(f"Taxable Gain P2:              £{taxable_p2:,.2f}\n")
        f.write(f"Tax Due P1 (@ 20%):           £{cgt_due_p1:,.2f}\n")
        f.write(f"Tax Due P2 (@ 24%):           £{cgt_due_p2:,.2f}\n")
        f.write(f"TOTAL ESTIMATED CGT:          £{total_cgt_due:,.2f}\n")

    # File 2: Audit Log
    # Columns: TIME_UTC, COIN, ORIGINAL_SELL_ID, MATCH_TYPE, MATCHED_WITH_ID, QTY_SOLD_CHUNK, PROCEEDS_GBP, COST_BASIS_GBP, GAIN_GBP, TAX_PERIOD
    audit_rows = []
    for entry in audit_log:
        if entry.get('TAX_PERIOD') == 'OUT_OF_SCOPE':
             continue # Instructions say "Only process Audit Log entries where Sell_Date is between..."

        audit_rows.append({
            'TIME_UTC': entry['TIME_UTC'],
            'COIN': entry['COIN'],
            'ORIGINAL_SELL_ID': entry['ORIGINAL_SELL_ID'],
            'MATCH_TYPE': entry['MATCH_TYPE'],
            'MATCHED_WITH_ID': entry['MATCHED_WITH_ID'],
            'QTY_SOLD_CHUNK': entry['QTY_SOLD_CHUNK'],
            'PROCEEDS_GBP': entry['PROCEEDS_GBP'],
            'COST_BASIS_GBP': entry['COST_BASIS_GBP'],
            'GAIN_GBP': entry['GAIN_GBP'],
            'TAX_PERIOD': entry['TAX_PERIOD']
        })

    df_audit = pd.DataFrame(audit_rows)
    # Sort by time
    if not df_audit.empty:
         # Rounding for output
         cols = ['QTY_SOLD_CHUNK', 'PROCEEDS_GBP', 'COST_BASIS_GBP', 'GAIN_GBP']
         for c in cols:
             df_audit[c] = df_audit[c].apply(lambda x: float(round(x, 2)))

         df_audit.to_csv(os.path.join(OUTPUT_DIR, "Audit_Log.csv"), index=False)
    else:
         with open(os.path.join(OUTPUT_DIR, "Audit_Log.csv"), "w") as f:
             f.write("No CGT events in Tax Year")

    # File 3: Closing Holdings
    holdings_rows = []
    for coin, pool in pools.items():
        if pool.total_qty > 0:
            avg = pool.total_cost_gbp / pool.total_qty
            holdings_rows.append({
                'COIN': coin,
                'QTY_HELD': float(pool.total_qty),
                'POOL_COST_GBP': float(round(pool.total_cost_gbp, 2)),
                'AVG_COST_PER_COIN': float(round(avg, 2))
            })

    df_holdings = pd.DataFrame(holdings_rows)
    df_holdings.to_csv(os.path.join(OUTPUT_DIR, "Closing_Holdings.csv"), index=False)

    print("Done. Files generated in Tax Files directory.")

if __name__ == "__main__":
    main()
