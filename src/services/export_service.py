# src/services/export_service.py
"""Export Service - generates Excel/CSV files and delivers via WhatsApp"""

import os
import csv
import logging
import boto3
from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from services.database import Database
from services.whatsapp_client import WhatsAppClient

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ.get('GENERATED_FILES_BUCKET', 'kashia-generated-files-dev')


class ExportService:
    """Generates Excel/CSV exports and delivers them via WhatsApp"""

    def __init__(self, database=None):
        self.db = database or Database()
        self.s3 = boto3.client('s3')
        self.whatsapp = WhatsAppClient()

    def generate_monthly_excel(self, phone_number, period="month"):
        """
        Generate a professional monthly Excel report with 3 sheets.
        Returns: (filepath, filename) or None
        """
        now = datetime.now()

        if period == "month":
            start_date = now.strftime('%Y-%m-01')
            end_date = now.strftime('%Y-%m-%d')
            period_label = now.strftime('%B_%Y')
        elif period == "week":
            monday = now - timedelta(days=now.weekday())
            start_date = monday.strftime('%Y-%m-%d')
            end_date = now.strftime('%Y-%m-%d')
            period_label = f"Week_{start_date}"
        else:
            start_date = now.strftime('%Y-%m-01')
            end_date = now.strftime('%Y-%m-%d')
            period_label = now.strftime('%B_%Y')

        transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        if not transactions:
            return None

        wb = Workbook()

        # ---- SHEET 1: Transactions ----
        ws1 = wb.active
        ws1.title = "Transactions"

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        money_format = '#,##0'
        income_fill = PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid")
        expense_fill = PatternFill(start_color="FFF3E0", end_color="FFF3E0", fill_type="solid")

        headers = ['Date', 'Description', 'Type', 'Amount (NGN)', 'Category', 'Vendor']
        for col, header in enumerate(headers, 1):
            cell = ws1.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
            cell.border = border

        for row_idx, tx in enumerate(sorted(transactions, key=lambda x: x.get('date', '')), 2):
            ws1.cell(row=row_idx, column=1, value=tx.get('date', ''))
            ws1.cell(row=row_idx, column=2, value=tx.get('description', ''))
            ws1.cell(row=row_idx, column=3, value=tx.get('type', '').title())

            amount_cell = ws1.cell(row=row_idx, column=4, value=int(tx.get('amount', 0)))
            amount_cell.number_format = money_format

            ws1.cell(row=row_idx, column=5, value=tx.get('category', ''))
            ws1.cell(row=row_idx, column=6, value=tx.get('vendor', ''))

            fill = income_fill if tx.get('type') == 'income' else expense_fill
            for col in range(1, 7):
                ws1.cell(row=row_idx, column=col).fill = fill
                ws1.cell(row=row_idx, column=col).border = border

        for col in range(1, 7):
            max_length = max(
                len(str(ws1.cell(row=r, column=col).value or ''))
                for r in range(1, ws1.max_row + 1)
            )
            ws1.column_dimensions[get_column_letter(col)].width = min(max_length + 4, 35)

        # ---- SHEET 2: Summary ----
        ws2 = wb.create_sheet("Summary")

        income = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'income')
        expenses = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'expense')
        profit = income - expenses

        ws2.cell(row=1, column=1, value="Financial Summary").font = Font(bold=True, size=14)
        ws2.cell(row=2, column=1, value=f"Period: {start_date} to {end_date}")

        summary_data = [
            ('Total Income', income),
            ('Total Expenses', expenses),
            ('Net Profit/Loss', profit),
            ('Total Transactions', len(transactions)),
        ]

        ws2.cell(row=4, column=1, value="Metric").font = Font(bold=True)
        ws2.cell(row=4, column=2, value="Amount (NGN)").font = Font(bold=True)

        for i, (label, value) in enumerate(summary_data, 5):
            ws2.cell(row=i, column=1, value=label)
            cell = ws2.cell(row=i, column=2, value=value)
            if isinstance(value, int):
                cell.number_format = money_format

        ws2.column_dimensions['A'].width = 25
        ws2.column_dimensions['B'].width = 20

        # ---- SHEET 3: Category Breakdown ----
        ws3 = wb.create_sheet("Category Breakdown")

        categories = {}
        for tx in transactions:
            if tx.get('type') == 'expense':
                cat = tx.get('category', 'Other')
                categories[cat] = categories.get(cat, 0) + int(tx.get('amount', 0))

        sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)

        ws3.cell(row=1, column=1, value="Expense Breakdown by Category").font = Font(bold=True, size=14)

        cat_headers = ['Category', 'Amount (NGN)', 'Percentage', 'Transactions']
        for col, header in enumerate(cat_headers, 1):
            cell = ws3.cell(row=3, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill

        total_expense = sum(categories.values())
        for i, (cat, amount) in enumerate(sorted_cats, 4):
            ws3.cell(row=i, column=1, value=cat)
            ws3.cell(row=i, column=2, value=amount).number_format = money_format
            pct = (amount / total_expense * 100) if total_expense > 0 else 0
            ws3.cell(row=i, column=3, value=f"{pct:.1f}%")
            count = len([tx for tx in transactions if tx.get('category') == cat and tx.get('type') == 'expense'])
            ws3.cell(row=i, column=4, value=count)

        ws3.column_dimensions['A'].width = 25
        ws3.column_dimensions['B'].width = 18
        ws3.column_dimensions['C'].width = 12
        ws3.column_dimensions['D'].width = 14

        filename = f"Kashia_Report_{period_label}.xlsx"
        filepath = f"/tmp/{filename}"
        wb.save(filepath)

        logger.info(f"Excel report generated: {filepath}")
        return filepath, filename

    def generate_csv(self, phone_number, period="all"):
        """Generate a CSV export of all transactions."""
        if period == "all":
            transactions = self.db.get_transactions(phone_number, limit=1000)
        else:
            now = datetime.now()
            start_date = now.strftime('%Y-%m-01')
            end_date = now.strftime('%Y-%m-%d')
            transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

        if not transactions:
            return None, None

        filename = f"Kashia_Transactions_{datetime.now().strftime('%Y%m%d')}.csv"
        filepath = f"/tmp/{filename}"

        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['Date', 'Description', 'Type', 'Amount', 'Category', 'Sub-Category', 'Vendor'])

            for tx in sorted(transactions, key=lambda x: x.get('date', '')):
                writer.writerow([
                    tx.get('date', ''),
                    tx.get('description', ''),
                    tx.get('type', ''),
                    tx.get('amount', 0),
                    tx.get('category', ''),
                    tx.get('sub_category', ''),
                    tx.get('vendor', ''),
                ])

        logger.info(f"CSV generated: {filepath}")
        return filepath, filename

    def generate_contacts_export(self, phone_number):
        """Export all contacts as Excel."""
        contacts = self.db.get_contacts(phone_number)

        if not contacts:
            return None, None

        wb = Workbook()
        ws = wb.active
        ws.title = "Contacts"

        headers = ['Name', 'Type', 'Total Paid (NGN)', 'Total Received (NGN)', 'Transactions', 'Last Transaction']
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill

        for i, contact in enumerate(contacts, 2):
            ws.cell(row=i, column=1, value=contact.get('name', ''))
            ws.cell(row=i, column=2, value=contact.get('type', '').title())
            ws.cell(row=i, column=3, value=int(contact.get('total_paid', 0)))
            ws.cell(row=i, column=4, value=int(contact.get('total_received', 0)))
            ws.cell(row=i, column=5, value=int(contact.get('transaction_count', 0)))
            ws.cell(row=i, column=6, value=contact.get('last_transaction_date', ''))

        for col in range(1, 7):
            ws.column_dimensions[get_column_letter(col)].width = 18

        filename = f"Kashia_Contacts_{datetime.now().strftime('%Y%m%d')}.xlsx"
        filepath = f"/tmp/{filename}"
        wb.save(filepath)

        return filepath, filename

    def upload_to_s3(self, filepath, filename):
        """Upload a file to S3 and return a presigned URL (24hr expiry)."""
        try:
            s3_key = f"exports/{datetime.now().strftime('%Y%m%d')}/{filename}"
            self.s3.upload_file(filepath, BUCKET_NAME, s3_key)

            presigned_url = self.s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': BUCKET_NAME, 'Key': s3_key},
                ExpiresIn=86400
            )

            logger.info(f"Uploaded to S3: {s3_key}")
            return presigned_url

        except Exception as e:
            logger.error(f"S3 upload error: {e}")
            return None

    def deliver_file(self, phone_number, filepath, filename, caption=""):
        """Full pipeline: upload to S3 then send to user via WhatsApp."""
        url = self.upload_to_s3(filepath, filename)
        if not url:
            self.whatsapp.send_text(phone_number, "Sorry, couldn't generate the file. Please try again.")
            return False

        success = self.whatsapp.send_document(phone_number, url, filename, caption)

        try:
            os.remove(filepath)
        except:
            pass

        return success

    def handle_export_request(self, phone_number, export_type):
        """
        Handle an export request end-to-end.
        Args: export_type: "month", "csv", "contacts"
        Returns: list of response dicts
        """
        if export_type in ["month", "export_month", "1"]:
            result = self.generate_monthly_excel(phone_number)
            if result:
                filepath, filename = result
                self.deliver_file(phone_number, filepath, filename,
                                  caption="Your monthly report - open in Excel or Google Sheets!")
                return [{"type": "text", "content": "File sent! Check your chat for the Excel file."}]
            else:
                return [{"type": "text", "content": "No transactions this month yet."}]

        elif export_type in ["csv", "export_csv", "2", "full"]:
            filepath, filename = self.generate_csv(phone_number)
            if filepath:
                self.deliver_file(phone_number, filepath, filename,
                                  caption="Full transaction history (CSV)")
                return [{"type": "text", "content": "CSV file sent!"}]
            else:
                return [{"type": "text", "content": "No transactions to export."}]

        elif export_type in ["contacts", "export_contacts", "3"]:
            filepath, filename = self.generate_contacts_export(phone_number)
            if filepath:
                self.deliver_file(phone_number, filepath, filename,
                                  caption="Your contacts list")
                return [{"type": "text", "content": "Contacts list sent!"}]
            else:
                return [{"type": "text", "content": "No contacts to export yet."}]

        else:
            return [{"type": "text", "content": "Unknown export type. Try: export month, export csv, or export contacts"}]
