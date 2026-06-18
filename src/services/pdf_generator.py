# src/services/pdf_generator.py
"""PDF Generator - creates invoices, receipts, and financial statements"""

import os
import logging
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

from services.database import Database
from services.whatsapp_client import WhatsAppClient
from services.export_service import ExportService

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class PDFGenerator:
    """Generates professional PDF documents for Kashia users"""

    def __init__(self, database=None):
        self.db = database or Database()
        self.whatsapp = WhatsAppClient()
        self.export_service = ExportService(database=self.db)
        self.styles = getSampleStyleSheet()
        self._add_custom_styles()

    def _add_custom_styles(self):
        """Add custom styles for Kashia documents"""
        if 'KashiaTitle' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaTitle',
                parent=self.styles['Title'],
                fontSize=22,
                textColor=HexColor('#1a5276'),
                spaceAfter=10
            ))
        if 'KashiaHeading' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaHeading',
                parent=self.styles['Heading2'],
                fontSize=14,
                textColor=HexColor('#2c3e50'),
                spaceBefore=15,
                spaceAfter=8
            ))
        if 'KashiaBody' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaBody',
                parent=self.styles['Normal'],
                fontSize=10,
                leading=14,
                spaceAfter=6
            ))
        if 'KashiaSmall' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaSmall',
                parent=self.styles['Normal'],
                fontSize=8,
                textColor=HexColor('#666666'),
                spaceAfter=4
            ))
        if 'KashiaRight' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaRight',
                parent=self.styles['Normal'],
                fontSize=10,
                alignment=TA_RIGHT
            ))

    def generate_invoice(self, phone_number, customer_name, amount, description, items=None):
        """
        Generate a professional invoice PDF.

        Args:
            phone_number: user's phone (to get their business info)
            customer_name: who the invoice is for
            amount: total amount
            description: what the invoice is for
            items: optional list of dicts [{"description": "...", "quantity": 1, "amount": 50000}]

        Returns:
            (filepath, filename) or (None, None) on error
        """
        try:
            user = self.db.get_user(phone_number)
            business_name = user.get('business_name', 'My Business') if user else 'My Business'

            # Generate invoice number
            invoice_number = f"KSH-{datetime.now().strftime('%Y%m%d%H%M')}"

            filename = f"Invoice_{invoice_number}.pdf"
            filepath = f"/tmp/{filename}"

            doc = SimpleDocTemplate(
                filepath, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            story = []

            # Header
            story.append(Paragraph("INVOICE", self.styles['KashiaTitle']))
            story.append(Spacer(1, 5*mm))

            # Invoice details table (From | Invoice info)
            from_info = (
                f"<b>From:</b><br/>"
                f"{business_name}<br/>"
                f"Phone: {phone_number}"
            )

            invoice_info = (
                f"<b>Invoice #:</b> {invoice_number}<br/>"
                f"<b>Date:</b> {datetime.now().strftime('%d %B %Y')}<br/>"
                f"<b>Due:</b> On Receipt"
            )

            header_table = Table(
                [[Paragraph(from_info, self.styles['KashiaBody']),
                  Paragraph(invoice_info, self.styles['KashiaRight'])]],
                colWidths=[9*cm, 8*cm]
            )
            header_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            story.append(header_table)
            story.append(Spacer(1, 10*mm))

            # Bill To
            story.append(Paragraph("<b>Bill To:</b>", self.styles['KashiaBody']))
            story.append(Paragraph(customer_name, self.styles['KashiaBody']))
            story.append(Spacer(1, 10*mm))

            # Items table
            if items:
                table_data = [['Description', 'Qty', 'Amount (NGN)']]
                total = 0
                for item in items:
                    item_amount = int(item.get('amount', 0))
                    qty = int(item.get('quantity', 1))
                    total += item_amount
                    table_data.append([
                        item.get('description', ''),
                        str(qty),
                        f"{item_amount:,}"
                    ])
            else:
                table_data = [['Description', 'Qty', 'Amount (NGN)']]
                table_data.append([description, '1', f"{int(amount):,}"])
                total = int(amount)

            # Add total row
            table_data.append(['', 'TOTAL', f"NGN {total:,}"])

            items_table = Table(table_data, colWidths=[9*cm, 3*cm, 5*cm])
            items_table.setStyle(TableStyle([
                # Header row
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                # Body
                ('FONTSIZE', (0, 1), (-1, -2), 10),
                ('GRID', (0, 0), (-1, -2), 0.5, HexColor('#cccccc')),
                # Total row
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, -1), (-1, -1), 12),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ]))
            story.append(items_table)
            story.append(Spacer(1, 15*mm))

            # Payment details
            story.append(Paragraph("<b>Payment Details:</b>", self.styles['KashiaHeading']))
            story.append(Paragraph(
                "Please make payment to the account below:",
                self.styles['KashiaBody']
            ))
            story.append(Paragraph(
                "Bank: [Your Bank Name]<br/>"
                "Account Number: [Your Account Number]<br/>"
                "Account Name: [Your Account Name]",
                self.styles['KashiaBody']
            ))
            story.append(Spacer(1, 10*mm))

            # Terms
            story.append(Paragraph("<b>Terms &amp; Conditions:</b>", self.styles['KashiaSmall']))
            story.append(Paragraph(
                "Payment is due upon receipt. Late payments may attract additional charges.",
                self.styles['KashiaSmall']
            ))
            story.append(Spacer(1, 15*mm))

            # Footer
            story.append(Paragraph(
                "Generated by Kashia - AI Bookkeeping for Nigerian Businesses",
                self.styles['KashiaSmall']
            ))

            # Build PDF
            doc.build(story)
            logger.info(f"Invoice generated: {filepath}")
            return filepath, filename

        except Exception as e:
            logger.error(f"Error generating invoice: {e}")
            return None, None

    def generate_receipt(self, phone_number, transaction_id=None):
        """
        Generate a payment receipt PDF.

        Args:
            phone_number: user's phone
            transaction_id: specific transaction (or uses last one)

        Returns:
            (filepath, filename) or (None, None)
        """
        try:
            # Get the transaction
            if transaction_id:
                transactions = self.db.get_transactions(phone_number, limit=50)
                tx = next((t for t in transactions if t.get('transaction_id') == transaction_id), None)
            else:
                transactions = self.db.get_transactions(phone_number, limit=1)
                tx = transactions[0] if transactions else None

            if not tx:
                return None, None

            user = self.db.get_user(phone_number)
            business_name = user.get('business_name', 'My Business') if user else 'My Business'

            receipt_number = f"RCP-{datetime.now().strftime('%Y%m%d%H%M')}"
            filename = f"Receipt_{receipt_number}.pdf"
            filepath = f"/tmp/{filename}"

            doc = SimpleDocTemplate(
                filepath, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            story = []

            # Header
            story.append(Paragraph("PAYMENT RECEIPT", self.styles['KashiaTitle']))
            story.append(Spacer(1, 5*mm))

            # Receipt info
            story.append(Paragraph(f"<b>Receipt #:</b> {receipt_number}", self.styles['KashiaBody']))
            story.append(Paragraph(f"<b>Date:</b> {tx.get('date', datetime.now().strftime('%Y-%m-%d'))}", self.styles['KashiaBody']))
            story.append(Paragraph(f"<b>From:</b> {business_name}", self.styles['KashiaBody']))
            story.append(Spacer(1, 10*mm))

            # Transaction details
            amount = int(tx.get('amount', 0))
            tx_type = tx.get('type', 'expense')

            if tx_type == 'expense':
                story.append(Paragraph(f"<b>Paid To:</b> {tx.get('vendor', 'N/A')}", self.styles['KashiaBody']))
            else:
                story.append(Paragraph(f"<b>Received From:</b> {tx.get('vendor', 'N/A')}", self.styles['KashiaBody']))

            story.append(Spacer(1, 8*mm))

            # Amount (large, prominent)
            amount_style = ParagraphStyle(
                name='AmountStyle',
                parent=self.styles['Normal'],
                fontSize=24,
                fontName='Helvetica-Bold',
                alignment=TA_CENTER,
                spaceAfter=10
            )
            story.append(Paragraph(f"NGN {amount:,}", amount_style))
            story.append(Spacer(1, 8*mm))

            # Details table
            details = [
                ['Description', tx.get('description', 'N/A')],
                ['Category', tx.get('category', 'N/A')],
                ['Type', tx_type.title()],
                ['Date', tx.get('date', 'N/A')],
            ]

            details_table = Table(details, colWidths=[5*cm, 12*cm])
            details_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, HexColor('#eeeeee')),
            ]))
            story.append(details_table)
            story.append(Spacer(1, 20*mm))

            # Signature line
            story.append(Paragraph("_" * 40, self.styles['KashiaBody']))
            story.append(Paragraph("Authorized Signature", self.styles['KashiaSmall']))
            story.append(Spacer(1, 15*mm))

            # Footer
            story.append(Paragraph(
                "Thank you for your business!",
                self.styles['KashiaBody']
            ))
            story.append(Paragraph(
                "Generated by Kashia - AI Bookkeeping for Nigerian Businesses",
                self.styles['KashiaSmall']
            ))

            doc.build(story)
            logger.info(f"Receipt generated: {filepath}")
            return filepath, filename

        except Exception as e:
            logger.error(f"Error generating receipt: {e}")
            return None, None

    def generate_financial_statement(self, phone_number, period="month"):
        """
        Generate a professional financial statement PDF.

        Returns:
            (filepath, filename) or (None, None)
        """
        try:
            now = datetime.now()

            if period == "month":
                start_date = now.strftime('%Y-%m-01')
                end_date = now.strftime('%Y-%m-%d')
                period_label = now.strftime('%B %Y')
            else:
                start_date = now.strftime('%Y-01-01')
                end_date = now.strftime('%Y-%m-%d')
                period_label = f"Year {now.year}"

            transactions = self.db.get_transactions_by_period(phone_number, start_date, end_date)

            if not transactions:
                return None, None

            user = self.db.get_user(phone_number)
            business_name = user.get('business_name', 'My Business') if user else 'My Business'

            filename = f"Financial_Statement_{now.strftime('%B_%Y')}.pdf"
            filepath = f"/tmp/{filename}"

            doc = SimpleDocTemplate(
                filepath, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            story = []

            # Header
            story.append(Paragraph(business_name, self.styles['KashiaTitle']))
            story.append(Paragraph(f"Financial Statement - {period_label}", self.styles['KashiaHeading']))
            story.append(Paragraph(f"Period: {start_date} to {end_date}", self.styles['KashiaSmall']))
            story.append(Spacer(1, 10*mm))

            # Calculate totals
            income = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'income')
            expenses = sum(int(tx.get('amount', 0)) for tx in transactions if tx.get('type') == 'expense')
            profit = income - expenses

            # Executive Summary
            story.append(Paragraph("<b>Executive Summary</b>", self.styles['KashiaHeading']))

            summary_data = [
                ['', 'Amount (NGN)'],
                ['Total Income', f"{income:,}"],
                ['Total Expenses', f"({expenses:,})"],
                ['Net Profit / (Loss)', f"{profit:,}"],
            ]

            summary_table = Table(summary_data, colWidths=[10*cm, 6*cm])
            summary_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#cccccc')),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
            ]))
            story.append(summary_table)
            story.append(Spacer(1, 10*mm))

            # Expense Breakdown
            story.append(Paragraph("<b>Expense Breakdown by Category</b>", self.styles['KashiaHeading']))

            categories = {}
            for tx in transactions:
                if tx.get('type') == 'expense':
                    cat = tx.get('category', 'Other')
                    categories[cat] = categories.get(cat, 0) + int(tx.get('amount', 0))

            sorted_cats = sorted(categories.items(), key=lambda x: x[1], reverse=True)

            cat_data = [['Category', 'Amount (NGN)', '%']]
            for cat, amount in sorted_cats:
                pct = int((amount / expenses * 100)) if expenses > 0 else 0
                cat_data.append([cat, f"{amount:,}", f"{pct}%"])
            cat_data.append(['TOTAL EXPENSES', f"{expenses:,}", '100%'])

            cat_table = Table(cat_data, colWidths=[8*cm, 5*cm, 3*cm])
            cat_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#34495e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#dddddd')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, -1), (-1, -1), 1, black),
            ]))
            story.append(cat_table)
            story.append(Spacer(1, 10*mm))

            # Transaction List (first 20)
            story.append(Paragraph("<b>Recent Transactions</b>", self.styles['KashiaHeading']))

            tx_data = [['Date', 'Description', 'Type', 'Amount (NGN)']]
            for tx in sorted(transactions, key=lambda x: x.get('date', ''), reverse=True)[:20]:
                tx_data.append([
                    tx.get('date', ''),
                    tx.get('description', '')[:35],
                    tx.get('type', '').title(),
                    f"{int(tx.get('amount', 0)):,}"
                ])

            tx_table = Table(tx_data, colWidths=[3*cm, 7*cm, 3*cm, 4*cm])
            tx_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#eeeeee')),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ]))
            story.append(tx_table)
            story.append(Spacer(1, 10*mm))

            # Footer
            story.append(Paragraph(f"Total Transactions: {len(transactions)}", self.styles['KashiaSmall']))
            story.append(Paragraph(
                f"Generated on {datetime.now().strftime('%d %B %Y at %H:%M')} by Kashia",
                self.styles['KashiaSmall']
            ))

            doc.build(story)
            logger.info(f"Financial statement generated: {filepath}")
            return filepath, filename

        except Exception as e:
            logger.error(f"Error generating financial statement: {e}")
            return None, None

    def deliver_pdf(self, phone_number, filepath, filename, caption=""):
        """Upload PDF to S3 and send via WhatsApp"""
        return self.export_service.deliver_file(phone_number, filepath, filename, caption)

    def handle_invoice_request(self, phone_number, customer_name, amount, description):
        """
        Handle full invoice generation and delivery.
        Returns: list of response dicts
        """
        result = self.generate_invoice(phone_number, customer_name, amount, description)

        if result and result[0]:
            filepath, filename = result
            self.deliver_pdf(phone_number, filepath, filename,
                            caption=f"Invoice for {customer_name} - NGN {int(amount):,}")
            return [{"type": "text", "content": (
                f"Invoice sent!\n\n"
                f"To: {customer_name}\n"
                f"Amount: NGN {int(amount):,}\n"
                f"For: {description}\n\n"
                f"Check your chat for the PDF file."
            )}]
        else:
            return [{"type": "text", "content": "Sorry, couldn't generate the invoice. Please try again."}]

    def handle_statement_request(self, phone_number):
        """
        Handle financial statement generation and delivery.
        Returns: list of response dicts
        """
        result = self.generate_financial_statement(phone_number)

        if result and result[0]:
            filepath, filename = result
            self.deliver_pdf(phone_number, filepath, filename,
                            caption="Your Financial Statement - ready for your accountant!")
            return [{"type": "text", "content": "Financial statement sent! Check your chat for the PDF."}]
        else:
            return [{"type": "text", "content": "No transactions found for this period. Record some transactions first!"}]

    def handle_receipt_request(self, phone_number):
        """
        Handle receipt generation for last transaction.
        Returns: list of response dicts
        """
        result = self.generate_receipt(phone_number)

        if result and result[0]:
            filepath, filename = result
            self.deliver_pdf(phone_number, filepath, filename,
                            caption="Payment Receipt")
            return [{"type": "text", "content": "Receipt sent! Check your chat for the PDF."}]
        else:
            return [{"type": "text", "content": "No recent transaction found to generate a receipt for."}]
