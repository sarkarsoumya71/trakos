"""Monthly sheet schema, stable identities, and spending dashboards."""
import re
import uuid
from datetime import datetime
from decimal import Decimal

import gspread
import time


class QuotaRetryClient(gspread.HTTPClient):
    """Bounded retries only for rejected quota requests, not ambiguous writes."""
    def request(self, *args, **kwargs):
        for attempt in range(8):
            try:
                return super().request(*args, **kwargs)
            except gspread.exceptions.APIError as exc:
                if exc.code != 429 or attempt == 7:
                    raise
                time.sleep(min(2 ** (attempt + 1), 32))

HEADERS = ['Date', 'Time', 'Amount', 'Description', 'Category', 'Payment Method',
           'Raw Input', 'Bank Message', 'Entry ID', 'Status', 'Treatment', 'Source']
INVESTMENTS = {'Financial Investment', 'Business Investment'}
EXCLUDED = {'Excluded', 'Duplicate'}


def paise(value):
    n = Decimal(str(value).replace(',', '').replace('\u20b9', '').strip()) * 100
    if not n.is_finite() or n < 0 or n != n.to_integral_value():
        raise ValueError('Invalid amount')
    return int(n)


def sms_id(marker):
    return 'S' + marker.split(':')[-1].strip('[]')


def normalize_row(source):
    row = list(source) + [''] * max(0, 12 - len(source))
    row = row[:12]
    marker = re.search(r'\[(trakos-sms:[a-f0-9]{32})\]', str(row[6]))
    if marker:
        row[7] = str(row[6]).replace(marker[0], '').strip()
        row[6], row[8], row[11] = '', sms_id(marker[1]), 'SMS'
    if not row[8]:
        row[8] = 'M' + uuid.uuid4().hex
    if re.fullmatch(r'(?:CBI|HDFC) Transaction', str(row[3]), re.I):
        row[3] = ''
    if str(row[4]).casefold() in ('other', 'others'):
        row[4] = ''
    row[9] = row[9] or ('Confirmed' if row[4] and row[3] else 'Needs details')
    row[10] = 'Investment' if row[4] in INVESTMENTS else (row[10] or 'Expense')
    row[11] = row[11] or 'Manual'
    return row


def needs_review(row):
    return row[9] not in EXCLUDED and (not row[4] or row[9] in ('Needs details', 'Possible duplicate'))


def near_manual_matches(tx, records):
    """Rounded manual values are candidates, never proof of duplication."""
    return [r for r in records if r['date'] == tx['occurred_at'][:10]
            and r.get('sms_id') is None and r.get('status') not in EXCLUDED
            and abs(r['amount_paise'] - tx['amount_paise']) <= 200]


def ensure_layout(ws, categories):
    headers = ws.row_values(1)
    if headers[:12] == HEADERS:
        return
    if headers[:7] != HEADERS[:7]:
        raise ValueError('Unexpected monthly headers; cannot migrate')
    # Read only the original data area; the old H:I summary is replaced by an overview.
    old = ws.get(f'A2:G{ws.row_count}')
    rows = []
    for source in old:
        if not source or not source[0]:
            rows.append([''] * 12)
            continue
        row = normalize_row(source)
        row[2] = paise(row[2]) / 100
        date = datetime.strptime(str(row[0]), '%d/%m/%Y')
        if date.strftime('%B %Y') != ws.title:
            raise ValueError('Date does not match the month')
        row[0] = (date - datetime(1899, 12, 30)).days
        rows.append(row)
    if ws.col_count < 12:
        ws.resize(cols=12)
    # One atomic update replaces the old summary and data, including stable identities.
    def cell(v):
        return {'userEnteredValue': {'numberValue': v} if isinstance(v, (int, float))
                else {'stringValue': str(v)}}
    ws.spreadsheet.batch_update({'requests': [{'updateCells': {
        'range': {'sheetId': ws.id, 'startRowIndex': 0, 'endRowIndex': max(len(rows)+1, 18),
                  'startColumnIndex': 0, 'endColumnIndex': 12},
        'rows': [{'values': [cell(v) for v in row]} for row in [HEADERS] + rows],
        'fields': 'userEnteredValue'}}]})
    format_month(ws, categories)
    ensure_dashboard(ws.spreadsheet, ws, categories)


def format_month(ws, categories):
    grid = {'sheetId': ws.id, 'startRowIndex': 1, 'endRowIndex': ws.row_count}
    requests = [
        {'updateSheetProperties': {'properties': {'sheetId': ws.id, 'gridProperties': {'frozenRowCount': 1}}, 'fields': 'gridProperties.frozenRowCount'}},
        {'repeatCell': {'range': {**grid, 'startColumnIndex': 0, 'endColumnIndex': 12},
            'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT', 'wrapStrategy': 'CLIP'}},
            'fields': 'userEnteredFormat.horizontalAlignment,userEnteredFormat.wrapStrategy'}},
        {'repeatCell': {'range': {'sheetId': ws.id, 'startRowIndex': 0, 'endRowIndex': 1, 'endColumnIndex': 12},
            'cell': {'userEnteredFormat': {'backgroundColor': {'red': .93, 'green': .93, 'blue': .93},
                'textFormat': {'bold': True, 'foregroundColor': {'red': 0, 'green': 0, 'blue': 0}}}},
            'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat'}},
        {'repeatCell': {'range': {**grid, 'startColumnIndex': 0, 'endColumnIndex': 1}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd/mm/yyyy'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': {**grid, 'startColumnIndex': 2, 'endColumnIndex': 3}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00'}}}, 'fields': 'userEnteredFormat.numberFormat'}},
        {'setBasicFilter': {'filter': {'range': {'sheetId': ws.id, 'endRowIndex': ws.row_count, 'endColumnIndex': 12}}}},
        {'setDataValidation': {'range': {**grid, 'startColumnIndex': 4, 'endColumnIndex': 5}, 'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': c} for c in categories]}, 'strict': True, 'showCustomUi': True}}},
    ]
    for start, end, width in [(0,1,105),(1,2,70),(2,3,110),(3,4,240),(4,5,170),(5,6,125),(6,8,330),(8,9,90),(9,11,135),(11,12,90)]:
        requests.append({'updateDimensionProperties': {'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': start, 'endIndex': end}, 'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
    # Stable IDs stay available for code without cluttering the working view.
    requests.append({'updateDimensionProperties': {'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': 8, 'endIndex': 9}, 'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}})
    requests.append({'updateDimensionProperties': {'range': {'sheetId': ws.id, 'dimension': 'ROWS', 'startIndex': 0, 'endIndex': ws.row_count}, 'properties': {'pixelSize': 28}, 'fields': 'pixelSize'}})
    ws.spreadsheet.batch_update({'requests': requests})


def ensure_dashboard(sh, ws, categories):
    title = ws.title + ' Overview'
    try:
        dash = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        dash = sh.add_worksheet(title=title, rows=max(200, ws.row_count), cols=13)
    ref = "'" + ws.title.replace("'", "''") + "'!"
    expense_cats = [c for c in categories if c not in INVESTMENTS]
    values = [[ws.title + ' spending', 'Amount'], ['Confirmed spending', f'=SUMIFS({ref}C2:C,{ref}J2:J,"Confirmed",{ref}K2:K,"Expense")'],
              ['Needs details', f'=SUMIF({ref}J2:J,"Needs details",{ref}C2:C)'],
              ['Possible duplicates (not counted)', f'=SUMIF({ref}J2:J,"Possible duplicate",{ref}C2:C)'],
              ['Investments (outside spending)', f'=SUMIFS({ref}C2:C,{ref}K2:K,"Investment",{ref}J2:J,"Confirmed")'],
              ['', ''], ['Category', 'Confirmed spending']]
    for i, cat in enumerate(expense_cats, 8):
        values.append([cat, f'=SUMIFS({ref}C2:C,{ref}E2:E,A{i},{ref}J2:J,"Confirmed",{ref}K2:K,"Expense")'])
    dash.update('A1:B' + str(len(values)), values, value_input_option='USER_ENTERED')
    dash.update('D1:E3', [['Explore a category', 'Subscriptions'], ['Total in this category', f'=SUMIFS({ref}C2:C,{ref}E2:E,E1,{ref}J2:J,"Confirmed")'], ['Only confirmed entries appear below', '']], value_input_option='USER_ENTERED')
    dash.update('D5', [[f'=IFERROR(QUERY({ref}A1:L,"select D,sum(C) where E = \'"&E1&"\' and J = \'Confirmed\' group by D label D \'Purchase / service\',sum(C) \'Amount\'",1),"No confirmed entries")']], value_input_option='USER_ENTERED')
    requests = [
        {'setDataValidation': {'range': {'sheetId': dash.id, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 4, 'endColumnIndex': 5}, 'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': c} for c in categories]}, 'strict': True, 'showCustomUi': True}}},
        {'repeatCell': {'range': {'sheetId': dash.id, 'endRowIndex': 1, 'endColumnIndex': 5}, 'cell': {'userEnteredFormat': {'backgroundColor': {'red': .93, 'green': .93, 'blue': .93}, 'textFormat': {'bold': True}}}, 'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat'}},
        {'updateDimensionProperties': {'range': {'sheetId': dash.id, 'dimension': 'COLUMNS', 'endIndex': 5}, 'properties': {'pixelSize': 200}, 'fields': 'pixelSize'}},
        {'updateDimensionProperties': {'range': {'sheetId': dash.id, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 1}, 'properties': {'pixelSize': 280}, 'fields': 'pixelSize'}},
    ]
    for col in (1, 4):
        requests.append({'repeatCell': {'range': {'sheetId': dash.id, 'startRowIndex': 1, 'startColumnIndex': col, 'endColumnIndex': col+1}, 'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '"₹"#,##0.00'}}}, 'fields': 'userEnteredFormat.numberFormat'}})
    meta = sh.fetch_sheet_metadata({'fields': 'sheets(properties(sheetId),charts(chartId))'})
    charts = next((s.get('charts', []) for s in meta['sheets'] if s['properties']['sheetId'] == dash.id), [])
    spec = {'title': ws.title + ' — confirmed spending', 'subtitle': 'Investments and entries awaiting details are shown separately', 'fontName': 'Arial', 'pieChart': {
        'legendPosition': 'RIGHT_LEGEND', 'pieHole': .45,
        'domain': {'sourceRange': {'sources': [{'sheetId': dash.id, 'startRowIndex': 7, 'endRowIndex': len(values), 'startColumnIndex': 0, 'endColumnIndex': 1}]}},
        'series': {'sourceRange': {'sources': [{'sheetId': dash.id, 'startRowIndex': 7, 'endRowIndex': len(values), 'startColumnIndex': 1, 'endColumnIndex': 2}]}}}}
    if not charts:
        requests.append({'addChart': {'chart': {'spec': spec, 'position': {'overlayPosition': {'anchorCell': {'sheetId': dash.id, 'rowIndex': 1, 'columnIndex': 6}, 'widthPixels': 700, 'heightPixels': 430}}}}})
    else:
        requests.append({'updateChartSpec': {'chartId': charts[0]['chartId'], 'spec': spec}})
    sh.batch_update({'requests': requests})


def data_end_column(ws):
    """Old tabs have seven data columns plus a two-column summary."""
    return "L" if ws.col_count >= 12 else "G"
