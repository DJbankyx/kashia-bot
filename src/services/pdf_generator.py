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

    def _generate_doc_number(self, phone_number, doc_type='INV'):
        """Generate a business-branded document number.
        Format: {INITIALS}-{COUNTER:05d} (e.g. BFH-00001)
        doc_type: 'INV' for invoice, 'RCP' for receipt
        """
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'Kashia') if user else 'Kashia'
        
        # Generate initials from business name (first letter of each word)
        words = business_name.split()
        if len(words) >= 2:
            initials = ''.join(w[0].upper() for w in words if w)[:4]
        else:
            initials = business_name[:3].upper()
        
        # Get and increment counter
        counter_field = f'{doc_type.lower()}_counter'
        current = int(user.get(counter_field, 0)) if user else 0
        new_count = current + 1
        
        # Save incremented counter
        try:
            self.db.update_user_field(phone_number, counter_field, new_count)
        except Exception:
            pass
        
        return f"{initials}-{new_count:05d}"


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
        if 'KashiaAmount' not in self.styles.byName:
            self.styles.add(ParagraphStyle(
                name='KashiaAmount',
                parent=self.styles['Normal'],
                fontSize=24,
                fontName='Helvetica-Bold',
                alignment=TA_CENTER,
                spaceAfter=10
            ))

    def generate_invoice(self, phone_number, customer_name, amount, description, items=None, discount=None, tax=None):
        """
        Generate a professional invoice PDF.

        Args:
            phone_number: user's phone (to get their business info)
            customer_name: who the invoice is for
            amount: total amount
            description: what the invoice is for
            items: optional list of dicts [{"description": "...", "quantity": 1, "amount": 50000}]
            discount: optional dict {"amount": 5000, "percent": 5, "type": "given"}
            tax: optional dict {"amount": 18750, "percent": 7.5, "type": "VAT"}

        Returns:
            (filepath, filename) or (None, None) on error
        """
        try:
            user = self.db.get_user(phone_number)
            business_name = user.get('business_name', 'My Business') if user else 'My Business'

            # Generate invoice number
            invoice_number = self._generate_doc_number(phone_number, "INV")

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
                table_data = [['Description', 'Qty', 'Unit Price', 'Amount (NGN)']]
                total = 0
                for item in items:
                    item_amount = int(item.get('amount', 0))
                    qty = int(item.get('quantity', 1))
                    total += item_amount
                    unit_price = item_amount // qty if qty > 0 else item_amount
                    table_data.append([
                        item.get('description', ''),
                        str(qty),
                        f"NGN {unit_price:,}",
                        f"NGN {item_amount:,}"
                    ])
            else:
                table_data = [['Description', 'Qty', 'Unit Price', 'Amount (NGN)']]
                # Try to extract quantity from description (e.g. "10 pairs of Nike socks")
                import re
                qty_match = re.match(r'^(\d+)\s*(pairs?|pieces?|pcs|cartons?|dozen|bags?|units?|boxes?)?\s*(?:of\s+)?(.+)', description, re.IGNORECASE)
                if qty_match:
                    qty = qty_match.group(1)
                    unit = qty_match.group(2) or 'pcs'
                    item_name = qty_match.group(3).strip()
                    unit_price = int(amount) // int(qty) if int(qty) > 0 else int(amount)
                    table_data.append([item_name, f"{qty} {unit}", f"NGN {unit_price:,}", f"NGN {int(amount):,}"])
                else:
                    table_data.append([description, '1', f"NGN {int(amount):,}", f"NGN {int(amount):,}"])
                total = int(amount)

            # Add subtotal, discount, tax, and total rows
            subtotal = total
            if discount or tax:
                table_data.append(['', '', 'Subtotal', f"NGN {subtotal:,}"])

            if discount:
                disc_amt = int(discount.get('amount', 0))
                disc_pct = discount.get('percent')
                disc_label = f"Discount ({disc_pct}%)" if disc_pct else "Discount"
                table_data.append(['', '', disc_label, f"- NGN {disc_amt:,}"])
                total = subtotal - disc_amt

            if tax:
                tax_amt = int(tax.get('amount', 0))
                tax_pct = tax.get('percent')
                tax_type = tax.get('type', 'Tax')
                tax_label = f"{tax_type} ({tax_pct}%)" if tax_pct else tax_type
                table_data.append(['', '', tax_label, f"+ NGN {tax_amt:,}"])
                total = total + tax_amt

            table_data.append(['', '', 'TOTAL', f"NGN {total:,}"])

            items_table = Table(table_data, colWidths=[7*cm, 2.5*cm, 3.5*cm, 4*cm])
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

            # Pull bank details from user profile
            bank_details = user.get('bank_details', {}) if user else {}
            bank_name = bank_details.get('bank_name', '') or user.get('bank_name', '')
            account_number = bank_details.get('account_number', '') or user.get('account_number', '')
            account_name = bank_details.get('account_name', business_name) or user.get('account_name', business_name)

            story.append(Paragraph(
                "Please make payment to:" if bank_name else "Please contact for payment details:",
                self.styles['KashiaBody']
            ))
            if bank_name:
                story.append(Paragraph(
                    f"Bank: {bank_name}<br/>"
                    f"Account Number: {account_number}<br/>"
                    f"Account Name: {account_name}",
                    self.styles['KashiaBody']
                ))
            else:
                story.append(Paragraph(
                    f"{business_name}<br/>"
                    f"Phone: {phone_number}",
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

            receipt_number = self._generate_doc_number(phone_number, "RCP")
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
            vendor = tx.get('vendor', '')

            if tx_type == 'expense':
                story.append(Paragraph(f"<b>Paid To:</b> {vendor or 'N/A'}", self.styles['KashiaBody']))
            else:
                story.append(Paragraph(f"<b>Received From:</b> {vendor or 'N/A'}", self.styles['KashiaBody']))

            story.append(Spacer(1, 8*mm))

            # Amount (large, prominent)
            story.append(Paragraph(f"NGN {amount:,}", self.styles['KashiaAmount']))
            story.append(Spacer(1, 8*mm))

            # Build clean description from transaction data
            item_desc = tx.get('product', '') or tx.get('item_type', '')
            brand = tx.get('brand', '')
            quantity = tx.get('quantity', '')
            unit_cost = tx.get('unit_cost', '')

            # Clean description line
            if item_desc and brand:
                clean_desc = f"{brand} {item_desc}"
            elif item_desc:
                clean_desc = item_desc
            else:
                # Fallback: clean the raw description
                raw_desc = tx.get('description', 'N/A')
                import re
                # Remove: "Sold/Bought", amounts, "to/from [Name]", "for [amount]", "on credit"
                clean_desc = raw_desc
                # Remove "Sold X" / "Bought X" prefix
                clean_desc = re.sub(r'^(?:sold|bought|purchased|received)\s+', '', clean_desc, flags=re.IGNORECASE)
                # Remove leading quantity + unit: "20 pairs of"
                clean_desc = re.sub(r'^\d+\s*(?:pairs?|pieces?|pcs|cartons?|dozen|bags?|units?|boxes?)\s*(?:of\s+)?', '', clean_desc, flags=re.IGNORECASE)
                # Remove "to/from [Name]" anywhere
                clean_desc = re.sub(r'\s+(?:to|from)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}', '', clean_desc, flags=re.IGNORECASE)
                # Remove amounts: "for 250000", "250,000", "100k each" etc
                clean_desc = re.sub(r'\s*(?:for\s+)?[\u20a6#]?\d[\d,]*[kKmM]?(?:\s+each)?\s*', ' ', clean_desc)
                # Remove "on credit"
                clean_desc = re.sub(r'\s*on\s+credit\s*', '', clean_desc, flags=re.IGNORECASE)
                # Remove trailing "to" or "from" left over
                clean_desc = re.sub(r'\s+(?:to|from)\s*$', '', clean_desc, flags=re.IGNORECASE)
                clean_desc = clean_desc.strip()[:60] or 'Goods/Services'

            # Remove vendor from clean_desc if still present
            if vendor and vendor.lower() in clean_desc.lower():
                import re
                clean_desc = re.sub(re.escape(vendor), '', clean_desc, flags=re.IGNORECASE).strip()

            # Details table with richer info
            details = [
                ['Item', clean_desc.title()],
                ['Date', tx.get('date', 'N/A')],
                ['Category', tx.get('category', 'N/A')],
            ]

            # Add quantity/unit cost if available
            if quantity:
                details.append(['Quantity', str(quantity)])
            if unit_cost:
                details.append(['Unit Price', f"NGN {int(unit_cost):,}"])

            # Add payment type
            payment_method = tx.get('payment_method', 'Cash').title() if tx.get('payment_method') else ('Cash' if tx_type == 'income' else 'Payment')
            details.append(['Payment', payment_method])

            # Add discount/tax breakdown if present in transaction
            discount_amt = tx.get('discount_amount')
            tax_amt = tx.get('tax_amount')
            subtotal = tx.get('subtotal')

            if discount_amt or tax_amt:
                if subtotal:
                    details.append(['Subtotal', f"NGN {int(subtotal):,}"])
                if discount_amt:
                    disc_pct = tx.get('discount_percent')
                    disc_label = f"Discount ({disc_pct}%)" if disc_pct else "Discount"
                    details.append([disc_label, f"- NGN {int(discount_amt):,}"])
                if tax_amt:
                    tax_pct = tx.get('tax_percent')
                    tax_type = tx.get('tax_type', 'Tax')
                    tax_label = f"{tax_type} ({tax_pct}%)" if tax_pct else tax_type
                    details.append([tax_label, f"+ NGN {int(tax_amt):,}"])

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

    def generate_financial_statement(self, phone_number, period="month", industry_class=None):
        """
        Generate a professional Profit & Loss statement PDF.
        Structure: Revenue → COGS → Gross Profit → Operating Expenses → Net Profit

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

            filename = f"PnL_Statement_{now.strftime('%B_%Y')}.pdf"
            filepath = f"/tmp/{filename}"

            doc = SimpleDocTemplate(
                filepath, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            story = []

            # ─── HEADER ───
            story.append(Paragraph(business_name, self.styles['KashiaTitle']))
            # Industry-specific title
            ind_titles = {'trading': 'Profit & Loss Statement', 'manufacturing': 'Manufacturing P&L Statement',
                         'services': 'Service Revenue Statement', 'hybrid': 'Combined P&L Statement'}
            story.append(Paragraph(ind_titles.get(industry, 'Profit & Loss Statement'), self.styles['KashiaHeading']))
            story.append(Paragraph(f"Period: {start_date} to {end_date} ({period_label})", self.styles['KashiaSmall']))
            story.append(Spacer(1, 8*mm))

            # ─── CATEGORIZE TRANSACTIONS ───
            # COGS categories based on industry
            from services.categorizer import INDUSTRY_CATEGORIES, PNL_LABELS
            industry = industry_class or 'trading'
            ind_config = INDUSTRY_CATEGORIES.get(industry, INDUSTRY_CATEGORIES['trading'])
            pnl_labels = PNL_LABELS.get(industry, PNL_LABELS['trading'])
            COGS_CATEGORIES = set(ind_config.get('cogs', ['Goods & Stock']))
            INCOME_CATEGORIES = set(ind_config.get('income', ['Sales & Income']))

            # Separate transactions
            income_txns = [tx for tx in transactions 
                         if tx.get('type') == 'income' and 'Debt payment' not in tx.get('description', '')]
            debt_payments = [tx for tx in transactions
                           if tx.get('type') == 'income' and 'Debt payment' in tx.get('description', '')]
            cogs_txns = [tx for tx in transactions
                        if tx.get('type') == 'expense' and tx.get('category', '') in COGS_CATEGORIES]
            opex_txns = [tx for tx in transactions
                        if tx.get('type') == 'expense' and tx.get('category', '') not in COGS_CATEGORIES]

            # Calculate totals
            total_revenue = sum(int(tx.get('amount', 0)) for tx in income_txns)
            total_cogs = sum(int(tx.get('amount', 0)) for tx in cogs_txns)
            total_opex = sum(int(tx.get('amount', 0)) for tx in opex_txns)
            total_debt_received = sum(int(tx.get('amount', 0)) for tx in debt_payments)
            gross_profit = total_revenue - total_cogs
            net_profit = gross_profit - total_opex
            gross_margin = int((gross_profit / total_revenue * 100)) if total_revenue > 0 else 0
            net_margin = int((net_profit / total_revenue * 100)) if total_revenue > 0 else 0

            # ─── REVENUE SECTION ───
            story.append(Paragraph(f"<b>{pnl_labels['revenue_title']}</b>", self.styles['KashiaHeading']))
            story.append(Paragraph(f"<i>{pnl_labels['revenue_subtitle']}</i>", self.styles['KashiaSmall']))

            # Group income by category
            revenue_cats = {}
            for tx in income_txns:
                cat = tx.get('category', 'Sales Revenue')
                revenue_cats[cat] = revenue_cats.get(cat, 0) + int(tx.get('amount', 0))

            rev_data = [['', 'Amount (\u20a6)']]
            for cat, amt in sorted(revenue_cats.items(), key=lambda x: x[1], reverse=True):
                rev_data.append([f"  {cat}", f"{amt:,}"])
            rev_data.append(['TOTAL REVENUE', f"{total_revenue:,}"])

            rev_table = Table(rev_data, colWidths=[10*cm, 6*cm])
            rev_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#27ae60')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#cccccc')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
            ]))
            story.append(rev_table)
            story.append(Spacer(1, 6*mm))

            # ─── COST OF GOODS SOLD ───
            story.append(Paragraph(f"<b>{pnl_labels['cogs_title']}</b>", self.styles['KashiaHeading']))
            story.append(Paragraph(f"<i>{pnl_labels['cogs_subtitle']}</i>", self.styles['KashiaSmall']))

            cogs_cats = {}
            for tx in cogs_txns:
                cat = tx.get('category', 'Goods & Stock')
                cogs_cats[cat] = cogs_cats.get(cat, 0) + int(tx.get('amount', 0))

            cogs_data = [['', 'Amount (\u20a6)']]
            if cogs_cats:
                for cat, amt in sorted(cogs_cats.items(), key=lambda x: x[1], reverse=True):
                    cogs_data.append([f"  {cat}", f"({amt:,})"])
            else:
                cogs_data.append(['  No COGS recorded', '-'])
            cogs_data.append(['TOTAL COGS', f"({total_cogs:,})"])

            cogs_table = Table(cogs_data, colWidths=[10*cm, 6*cm])
            cogs_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#e74c3c')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#cccccc')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
            ]))
            story.append(cogs_table)
            story.append(Spacer(1, 6*mm))

            # ─── GROSS PROFIT ───
            gp_color = '#27ae60' if gross_profit >= 0 else '#e74c3c'
            gp_data = [
                [pnl_labels['gross_title'], f"{gross_profit:,}"],
                ['Gross Margin', f"{gross_margin}%"],
            ]
            gp_table = Table(gp_data, colWidths=[10*cm, 6*cm])
            gp_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), HexColor('#ecf0f1')),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 11),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('TEXTCOLOR', (1, 0), (1, 0), HexColor(gp_color)),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('BOX', (0, 0), (-1, -1), 1.5, HexColor('#2c3e50')),
            ]))
            story.append(gp_table)
            story.append(Spacer(1, 8*mm))

            # ─── OPERATING EXPENSES ───
            story.append(Paragraph(f"<b>{pnl_labels['opex_title']}</b>", self.styles['KashiaHeading']))

            opex_cats = {}
            for tx in opex_txns:
                cat = tx.get('category', 'Other Expenses')
                opex_cats[cat] = opex_cats.get(cat, 0) + int(tx.get('amount', 0))

            opex_data = [['', 'Amount (\u20a6)']]
            if opex_cats:
                for cat, amt in sorted(opex_cats.items(), key=lambda x: x[1], reverse=True):
                    pct = int((amt / total_opex * 100)) if total_opex > 0 else 0
                    opex_data.append([f"  {cat}", f"({amt:,})"])
            else:
                opex_data.append(['  No operating expenses', '-'])
            opex_data.append(['TOTAL OPERATING EXPENSES', f"({total_opex:,})"])

            opex_table = Table(opex_data, colWidths=[10*cm, 6*cm])
            opex_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#f39c12')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#cccccc')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
            ]))
            story.append(opex_table)
            story.append(Spacer(1, 8*mm))

            # ─── NET PROFIT ═══
            np_color = '#27ae60' if net_profit >= 0 else '#e74c3c'
            np_data = [
                [pnl_labels['net_title'], f"{net_profit:,}"],
                ['Net Margin', f"{net_margin}%"],
            ]
            np_table = Table(np_data, colWidths=[10*cm, 6*cm])
            np_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, -1), white),
                ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (0, 0), 12),
                ('FONTSIZE', (0, 1), (-1, 1), 10),
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('TEXTCOLOR', (1, 0), (1, 0), HexColor('#2ecc71') if net_profit >= 0 else HexColor('#e74c3c')),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                ('BOX', (0, 0), (-1, -1), 2, HexColor('#2c3e50')),
            ]))
            story.append(np_table)
            story.append(Spacer(1, 8*mm))

            # ─── DEBT PAYMENTS RECEIVED (memo) ───
            if debt_payments:
                story.append(Paragraph("<b>MEMO: Debt Payments Received</b>", self.styles['KashiaHeading']))
                story.append(Paragraph(
                    f"Total debt collected this period: \u20a6{total_debt_received:,} ({len(debt_payments)} payments)",
                    self.styles['KashiaBody']
                ))
                story.append(Paragraph(
                    "<i>Note: Debt payments are cash collected from previously recorded credit sales. "
                    "They are not new revenue and are excluded from the P&L above.</i>",
                    self.styles['KashiaSmall']
                ))
                story.append(Spacer(1, 6*mm))

            # ─── TRANSACTION DETAILS ───
            display_txns = [tx for tx in transactions if 'Debt payment' not in tx.get('description', '')]
            story.append(Paragraph(f"<b>Transaction Details ({len(display_txns)} entries)</b>", self.styles['KashiaHeading']))

            tx_data = [['Date', 'Description', 'Category', 'Type', 'Amount']]
            for tx in display_txns:
                desc = self._clean_item_description(tx)
                amount = int(tx.get('amount', 0))
                qty = tx.get('quantity', '')
                unit_cost = tx.get('unit_cost', '')
                display_desc = desc
                if qty and unit_cost:
                    display_desc += f" x{qty} @ {int(unit_cost):,}"
                elif qty:
                    display_desc += f" x{qty}"
                tx_type = tx.get('type', '')
                category = tx.get('category', '')[:18]
                amount_str = f"{amount:,}" if tx_type == 'income' else f"({amount:,})"
                tx_data.append([
                    tx.get('date', ''),
                    display_desc[:40],
                    category,
                    tx_type.title()[:3],  # Inc / Exp
                    amount_str
                ])

            tx_table = Table(tx_data, colWidths=[2.2*cm, 5.5*cm, 3.5*cm, 1.5*cm, 3.3*cm])
            tx_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#34495e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
                ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#eeeeee')),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor('#f8f9fa')]),
            ]))
            story.append(tx_table)
            story.append(Spacer(1, 8*mm))

            # Footer
            story.append(Paragraph(
                f"Generated on {datetime.now().strftime('%d %B %Y at %H:%M')} by Kashia",
                self.styles['KashiaSmall']
            ))

            doc.build(story)
            logger.info(f"P&L statement generated: {filepath}")
            return filepath, filename

        except Exception as e:
            logger.error(f"Error generating financial statement: {e}")
            return None, None

    def deliver_pdf(self, phone_number, filepath, filename, caption=""):
        """Upload PDF to S3 and send via WhatsApp. Returns (success, s3_url)."""
        result = self.export_service.deliver_file(phone_number, filepath, filename, caption)
        if isinstance(result, tuple):
            return result
        return result, None

    def _clean_item_description(self, tx):
        """Build a clean item description from transaction data for invoices/receipts"""
        import re
        brand = tx.get('brand', '')
        product = tx.get('product', '') or tx.get('item_type', '')

        # Best case: brand + product both exist
        if brand and product:
            return f"{brand} {product}".strip().title()

        # If only brand exists, try to find product name from description
        raw_desc = tx.get('description', '')
        if brand and not product and raw_desc:
            # Try to find what comes after brand in the description
            # e.g. "Sold 20 pairs of Nike socks" → after "Nike" → "socks"
            match = re.search(rf'{re.escape(brand)}\s+(\w+)', raw_desc, re.IGNORECASE)
            if match:
                return f"{brand} {match.group(1)}".title()
            return brand.title()

        # Fallback: clean the raw description
        if raw_desc:
            desc = re.sub(r'^(sold|bought|paid|received|gave|sent|got)\s+', '', raw_desc, flags=re.IGNORECASE)
            desc = re.sub(r'₦?\d[\d,]*[kKmM]?', '', desc)
            desc = re.sub(r'(to|from)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?', '', desc)
            desc = re.sub(r'^\d+\s*(pairs?|pieces?|cartons?|bags?|packs?|bottles?)\s+(of\s+)?', '', desc, flags=re.IGNORECASE)
            desc = re.sub(r'^(for|of)\s+', '', desc, flags=re.IGNORECASE)
            desc = re.sub(r'\s+', ' ', desc).strip()
            return desc.title()[:45] if desc else 'Goods/Services'

        return 'Goods/Services'

    def handle_invoice_request(self, phone_number, customer_name, amount, description, discount=None, tax=None):
        """
        Handle full invoice generation and delivery.
        Returns: list of response dicts
        """
        result = self.generate_invoice(phone_number, customer_name, amount, description, discount=discount, tax=tax)

        if result and result[0]:
            filepath, filename = result
            delivered, s3_url = self.deliver_pdf(phone_number, filepath, filename,
                            caption=f"Invoice for {customer_name} - \u20a6{int(amount):,}")
            if not delivered:
                return [{"type": "text", "content": "⚠️ Invoice generated but delivery failed. Please try again."}]
            responses = [{"type": "text", "content": (
                f"✅ Invoice sent!\n\n"
                f"To: {customer_name}\n"
                f"Amount: \u20a6{int(amount):,}\n"
                f"For: {description}\n\n"
                f"📎 Check your chat for the PDF.\n\n"
                f"\ud83d\udd17 *Shareable link* (valid 24hrs):\n{s3_url}"
            )}]
            # Add forward-to-customer metadata
            if customer_name and customer_name.lower() != 'customer':
                responses.append({"type": "forward_prompt", "content": {
                    "customer_name": customer_name, "s3_url": s3_url, "filename": filename}})
            return responses
        else:
            return [{"type": "text", "content": "Sorry, couldn't generate the invoice. Please try again."}]

    def handle_multi_invoice_request(self, phone_number, transaction_ids):
        """Generate invoice from multiple transactions"""
        transactions = self.db.get_transactions(phone_number, limit=50)
        selected = [tx for tx in transactions if tx.get('transaction_id') in transaction_ids]

        if not selected:
            return [{"type": "text", "content": "No matching transactions found."}]

        # Build items list for the invoice
        items = []
        customer_name = ''
        for tx in selected:
            qty = int(tx.get('quantity', 1)) or 1
            amount = int(tx.get('amount', 0))
            brand = tx.get('brand', '')
            product = tx.get('product', '') or tx.get('item_type', '')
            desc = self._clean_item_description(tx)
            vendor = tx.get('vendor', '')
            if vendor and not customer_name:
                customer_name = vendor
            items.append({
                'description': desc,
                'quantity': qty,
                'amount': amount
            })

        if not customer_name:
            customer_name = 'Customer'

        total_amount = sum(item['amount'] for item in items)

        result = self.generate_invoice(phone_number, customer_name, total_amount, '', items=items)

        if result and result[0]:
            filepath, filename = result
            delivered, s3_url = self.deliver_pdf(phone_number, filepath, filename,
                            caption=f"Invoice for {customer_name} - \u20a6{total_amount:,}")
            if not delivered:
                return [{"type": "text", "content": "\u26a0\ufe0f Invoice generated but delivery failed. Please try again."}]
            responses = [{"type": "text", "content": (
                f"\u2705 Multi-item invoice sent!\n\n"
                f"To: {customer_name}\n"
                f"Items: {len(items)}\n"
                f"Total: \u20a6{total_amount:,}\n\n"
                f"\ud83d\udcce Check your chat for the PDF.\n\n"
                f"\ud83d\udd17 *Shareable link* (valid 24hrs):\n{s3_url}"
            )}]
            if customer_name and customer_name.lower() != 'customer':
                responses.append({"type": "forward_prompt", "content": {
                    "customer_name": customer_name, "s3_url": s3_url, "filename": filename}})
            return responses
        return [{"type": "text", "content": "Sorry, couldn't generate the invoice. Please try again."}]

    def handle_multi_receipt_request(self, phone_number, transaction_ids):
        """Generate receipt for specific transaction(s)"""
        transactions = self.db.get_transactions(phone_number, limit=50)
        selected = [tx for tx in transactions if tx.get('transaction_id') in transaction_ids]

        if not selected:
            return [{"type": "text", "content": "No matching transactions found."}]

        # For single transaction, use the standard receipt generator
        if len(selected) == 1:
            return self._generate_and_deliver_receipt(phone_number, selected[0])

        # For multiple, generate a combined receipt
        return self._generate_and_deliver_multi_receipt(phone_number, selected)

    def _generate_and_deliver_receipt(self, phone_number, tx):
        """Generate and deliver receipt for a single transaction"""
        result = self.generate_receipt(phone_number, transaction_id=tx.get('transaction_id'))
        if result and result[0]:
            filepath, filename = result
            delivered, s3_url = self.deliver_pdf(phone_number, filepath, filename, caption="Payment Receipt")
            if not delivered:
                return [{"type": "text", "content": "\u26a0\ufe0f Receipt generated but delivery failed."}]
            responses = [{"type": "text", "content": f"\u2705 Receipt sent! Check your chat for the PDF.\n\n\ud83d\udd17 *Shareable link* (valid 24hrs):\n{s3_url}"}]
            # Add forward prompt if there's a customer
            vendor = tx.get('vendor', '')
            if vendor and vendor.lower() not in {'unknown', 'sold', 'bought', 'paid', 'received', ''}:
                responses.append({"type": "forward_prompt", "content": {
                    "customer_name": vendor, "s3_url": s3_url, "filename": filename}})
            return responses
        return [{"type": "text", "content": "Couldn't generate receipt for that transaction."}]

    def _generate_and_deliver_multi_receipt(self, phone_number, transactions):
        """Generate a combined receipt for multiple transactions"""
        # Use the first transaction's date as receipt date
        user = self.db.get_user(phone_number)
        business_name = user.get('business_name', 'My Business') if user else 'My Business'

        receipt_number = self._generate_doc_number(phone_number, "RCP")
        filename = f"Receipt_{receipt_number}.pdf"
        filepath = f"/tmp/{filename}"

        try:
            doc = SimpleDocTemplate(
                filepath, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm
            )

            story = []
            story.append(Paragraph("PAYMENT RECEIPT", self.styles['KashiaTitle']))
            story.append(Spacer(1, 5*mm))
            story.append(Paragraph(f"<b>Receipt #:</b> {receipt_number}", self.styles['KashiaBody']))
            story.append(Paragraph(f"<b>Date:</b> {datetime.now().strftime('%d %B %Y')}", self.styles['KashiaBody']))
            story.append(Paragraph(f"<b>From:</b> {business_name}", self.styles['KashiaBody']))
            story.append(Spacer(1, 10*mm))

            # Items table
            table_data = [['#', 'Description', 'Qty', 'Amount (NGN)']]
            total = 0
            for idx, tx in enumerate(transactions, 1):
                amount = int(tx.get('amount', 0))
                total += amount
                qty = tx.get('quantity', '1')
                desc = self._clean_item_description(tx)
                table_data.append([str(idx), desc, str(qty), f"NGN {amount:,}"])

            table_data.append(['', '', 'TOTAL', f"NGN {total:,}"])

            items_table = Table(table_data, colWidths=[1.5*cm, 8*cm, 3*cm, 4.5*cm])
            items_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2c3e50')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -2), 0.5, HexColor('#cccccc')),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('LINEABOVE', (0, -1), (-1, -1), 1.5, black),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ]))
            story.append(items_table)
            story.append(Spacer(1, 15*mm))

            # Footer
            story.append(Paragraph("Thank you for your business!", self.styles['KashiaBody']))
            story.append(Paragraph("Generated by Kashia - AI Bookkeeping for Nigerian Businesses", self.styles['KashiaSmall']))

            doc.build(story)

            delivered, s3_url = self.deliver_pdf(phone_number, filepath, filename, caption=f"Receipt - {len(transactions)} items")
            if not delivered:
                return [{"type": "text", "content": "\u26a0\ufe0f Receipt generated but delivery failed."}]
            responses = [{"type": "text", "content": (
                f"\u2705 Combined receipt sent!\n\n"
                f"Items: {len(transactions)}\n"
                f"Total: \u20a6{total:,}\n\n"
                f"\ud83d\udcce Check your chat for the PDF.\n\n"
                f"\ud83d\udd17 *Shareable link* (valid 24hrs):\n{s3_url}"
            )}]
            # Add forward prompt — use first customer found
            customer_name = ''
            for tx in transactions:
                v = tx.get('vendor', '')
                if v and v.lower() not in {'unknown', 'sold', 'bought', 'paid', 'received', ''}:
                    customer_name = v
                    break
            if customer_name:
                responses.append({"type": "forward_prompt", "content": {
                    "customer_name": customer_name, "s3_url": s3_url, "filename": filename}})
            return responses

        except Exception as e:
            logger.error(f"Error generating multi-receipt: {e}")
            return [{"type": "text", "content": "Sorry, couldn't generate the receipt. Please try again."}]

    def handle_statement_request(self, phone_number):
        """
        Handle financial statement generation and delivery.
        Returns: list of response dicts
        """
        # Get user's industry class for tailored P&L
        user = self.db.get_user(phone_number)
        industry_class = user.get('industry_class', 'trading') if user else 'trading'
        result = self.generate_financial_statement(phone_number, industry_class=industry_class)

        if result and result[0]:
            filepath, filename = result
            delivered = self.deliver_pdf(phone_number, filepath, filename,
                            caption="Your Financial Statement - ready for your accountant!")
            if not delivered:
                return [{"type": "text", "content": "⚠️ Statement generated but delivery failed. Please try again."}]
            return [{"type": "text", "content": "✅ Financial statement sent! Check your chat for the PDF."}]
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
            delivered, s3_url = self.deliver_pdf(phone_number, filepath, filename,
                            caption="Payment Receipt")
            if not delivered:
                return [{"type": "text", "content": "⚠️ Receipt generated but delivery failed. Please try again."}]
            responses = [{"type": "text", "content": f"✅ Receipt sent! Check your chat for the PDF.\n\n🔗 *Shareable link* (valid 24hrs):\n{s3_url}"}]
            # Try to find customer for forward prompt
            transactions = self.db.get_transactions(phone_number, limit=1)
            if transactions:
                vendor = transactions[0].get('vendor', '')
                if vendor and vendor.lower() not in {'unknown', 'sold', 'bought', 'paid', 'received', ''}:
                    responses.append({"type": "forward_prompt", "content": {
                        "customer_name": vendor, "s3_url": s3_url, "filename": filename}})
            return responses
        else:
            return [{"type": "text", "content": "No recent transaction found to generate a receipt for."}]
