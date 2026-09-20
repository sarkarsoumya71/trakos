FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py sms_import.py sms_workflow.py sms_auto.py expense_reports.py expense_sheet.py expense_edit.py .

CMD ["python", "bot.py"]
