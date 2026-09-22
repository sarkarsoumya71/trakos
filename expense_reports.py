"""Exact calendar spending reports from the existing monthly expense tabs."""
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from expense_sheet import INVESTMENTS, EXCLUDED, SPENDING_STATUSES, data_end_column


def clean(text, limit=60):
    return ' '.join(str(text).split())[:limit]


def money(paise):
    return f'₹{Decimal(paise) / 100:,.2f}' if paise % 100 else f'₹{paise // 100:,}'


def period_starts(now):
    today = now.date()
    return {'today': today, 'week': today - timedelta(days=today.weekday()), 'month': today.replace(day=1)}


def read_spending(bot, now):
    """Read each relevant month once; never turn a read error into a partial total."""
    starts = period_starts(now)
    first = min(starts.values()).replace(day=1)
    last = now.date().replace(day=1)
    entries = []
    with bot.SHEET_LOCK:
        sh = bot.get_spreadsheet()
        for ws in sh.worksheets():
            try:
                month = datetime.strptime(ws.title, '%B %Y').date()
            except ValueError:
                continue
            if not first <= month <= last:
                continue
            # New tabs include status/treatment; legacy tabs end before their summary.
            rows = ws.get(f'A1:{data_end_column(ws)}{ws.row_count}')
            if not rows or rows[0][:5] != bot.HEADER_ROW[:5]:
                raise ValueError(f'Unexpected expense headers in {ws.title}')
            for index, row in enumerate(rows[1:], 2):
                if not any(str(value).strip() for value in row):
                    continue
                try:
                    date = datetime.strptime(row[0], '%d/%m/%Y').date()
                    if date.replace(day=1) != month:
                        raise ValueError('Date belongs to another month')
                    if not min(starts.values()) <= date <= now.date():
                        continue
                    paise = Decimal(str(row[2]).replace(',', '').strip()) * 100
                    if not paise.is_finite() or paise < 0 or paise != paise.to_integral_value():
                        raise ValueError('Invalid amount')
                    category = str(row[4]).strip() if len(row) > 4 else ''
                    status = row[9] if len(row)>9 else ('Needs details' if not category or category=='Other' else 'Confirmed')
                    treatment = row[10] if len(row)>10 else ('Investment' if category in INVESTMENTS else 'Expense')
                    entries.append({'date': date, 'paise': int(paise), 'category': '' if category=='Other' else category,
                                    'status':status,'treatment':treatment,
                                    'description': row[3] if len(row) > 3 else '',
                                    'sheet': ws.title, 'row': index})
                except (ValueError, InvalidOperation, IndexError) as exc:
                    raise ValueError(f'Check expense row {index} in {ws.title}') from exc
    periods = {}
    for key, start in starts.items():
        all_selected = [row for row in entries if start <= row['date'] <= now.date() and row['status'] not in EXCLUDED]
        selected = [row for row in all_selected if row['status'] in SPENDING_STATUSES and row['treatment']!='Investment']
        categories = Counter()
        for row in selected:
            categories[row['category']] += row['paise']
        periods[key] = {'start': start, 'total': sum(row['paise'] for row in selected),
                        'count': len(selected), 'categories': categories, 'entries': selected,
                        'pending':sum(row['paise'] for row in all_selected if row['status']=='Needs details'),
                        'possible_duplicates':sum(row['paise'] for row in all_selected if row['status']=='Possible duplicate'),
                        'return_pending':sum(row['paise'] for row in all_selected if row['status']=='Return pending'),
                        'refunded':sum(row['paise'] for row in all_selected if row['status']=='Refunded'),
                        'investments':sum(row['paise'] for row in all_selected if row['status']=='Confirmed' and row['treatment']=='Investment')}
    return {'as_of': now, 'periods': periods}


def render(snapshot, period=None, freshness='', nightly=False):
    now = snapshot['as_of']
    lines = [f"{'Daily spending report' if nightly else 'Spending check'} · {now:%d %b %Y, %H:%M} IST"]
    for key in ([period] if period else ['today', 'week', 'month']):
        data = snapshot['periods'][key]
        start = data['start']
        title = {'today': 'Today', 'week': 'This week (Monday–today)', 'month': now.strftime('%B %Y')}[key]
        if key != 'today':
            title += f' · {start:%d %b}–{now:%d %b}'
        lines.extend(['', title, f"Total: {money(data['total'])} · {data['count']} expense(s)"])
        categories = data['categories'].most_common()
        for category, amount in categories[:10]:
            lines.append(f'  {clean(category, 40)}: {money(amount)}')
        if data.get('pending'):
            lines.append(f"  Needs details (outside confirmed total): {money(data['pending'])} — /review")
        if data.get('possible_duplicates'):
            lines.append(f"  Possible duplicates held aside: {money(data['possible_duplicates'])}")
        if data.get('investments'):
            lines.append(f"  Investments (separate): {money(data['investments'])}")
        if data.get('return_pending'):
            lines.append(f"  Refund pending (still included): {money(data['return_pending'])} — /returns")
        if data.get('refunded'):
            lines.append(f"  Refunded purchases (not counted): {money(data['refunded'])}")
        if len(categories) > 10:
            lines.append(f"  Remaining categories: {money(sum(v for _, v in categories[10:]))}")
        if not data['count']:
            lines.append('No recorded expenses in this period.')
        if key == 'today' and data['entries']:
            lines.append('Today’s purchases (largest first):')
            entries = sorted(data['entries'], key=lambda row: -row['paise'])
            for row in entries[:8]:
                lines.append(f"  {money(row['paise'])} · {clean(row['description']) or 'Description unavailable'}")
            if len(entries) > 8:
                lines.append(f'  +{len(entries) - 8} more in /sheet')
    lines.extend(['', 'Totals cover recorded spending, not your available bank balance.'])
    if any(snapshot['periods'][key]['categories'].get('Other', 0) for key in ([period] if period else ['today', 'week', 'month'])):
        lines.append('Other groups expenses without a more specific category. /others shows their details.')
    if freshness:
        lines.append(freshness)
    lines.append('SMS updates depend on phone backups. /syncsms checks Drive now.')
    return '\n'.join(lines)
