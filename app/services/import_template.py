"""Builds the downloadable .xlsx import template (in memory)."""
import io

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation

HEADERS = ["first_name", "surname", "phone", "id_number", "id_type", "employee_code"]
TEXT_FORMAT = "@"  # Excel's "Text" format: keeps leading zeros
TEXT_COLUMNS = ("C", "D", "E", "F")  # phone, id_number, id_type, employee_code
EXAMPLES = [
    # fake people; the importer rejects these exact names if they are left in the file
    ["Example", "Person", "71234567", "000000000", "omang", "EX-001"],
    ["Example", "Traveller", "+27821234567", "A00000000", "passport", "EX-002"],
]


def build_template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for example in EXAMPLES:
        ws.append(example)

    # phone, id_number, id_type and employee_code are Text columns (whole column, so new rows are Text too)
    for letter in TEXT_COLUMNS:
        ws.column_dimensions[letter].number_format = TEXT_FORMAT
        ws.column_dimensions[letter].width = 18
    for letter in ("A", "B"):
        ws.column_dimensions[letter].width = 18
    for row in ws.iter_rows(min_row=2, max_row=1 + len(EXAMPLES), min_col=3, max_col=6):
        for cell in row:
            cell.number_format = TEXT_FORMAT
            cell.data_type = "s"

    # a dropdown for id_type (blank is allowed: the system then guesses and asks you to check)
    dropdown = DataValidation(type="list", formula1='"omang,passport"', allow_blank=True)
    ws.add_data_validation(dropdown)
    dropdown.add("E2:E2001")

    help_sheet = wb.create_sheet("Instructions")
    for line in [
        "How to use this template",
        "1. Delete the two example rows (Example Person, Example Traveller) and type your employees from row 2. Keep the header row.",
        "2. first_name and surname are required. The other columns are optional (employee_code is best for telling same-named people apart).",
        "3. phone: 71234567, 26771234567 or +26771234567. Other countries must start with + and the country code.",
        "4. id_number: the FULL Omang (9 digits) or passport number (5 to 15 letters/digits). It is stored encrypted. A number with the wrong length is accepted with a warning; a passport shorter than 5 characters is rejected.",
        "5. id_type: omang or passport. If you leave it blank, digits-only numbers are treated as an Omang and numbers with letters as a passport, and you are asked to check.",
        "6. The last 4 characters of the ID (in capitals) become the password that opens the employee's payslip PDF.",
        "7. phone, id_number, id_type and employee_code are formatted as Text so Excel keeps leading zeros. Do not change that.",
        "8. Only the first sheet (Employees) is read. Save as .xlsx (no macros) or CSV. The old column name 'omang' still works for id_number.",
        "9. Empty phone/id_number cells never erase what is already saved. Importing a file again with full ID numbers fills in the full number for people who only had the last 4 saved.",
    ]:
        help_sheet.append([line])
    help_sheet["A1"].font = Font(bold=True, size=13)
    help_sheet.column_dimensions["A"].width = 140

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
