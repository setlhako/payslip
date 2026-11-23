"""Make FAKE test files for the employee import. Run: python -m app.scripts.make_sample_import

Writes sample_import.xlsx and sample_import.csv in the project folder. Everything in them is invented
(fake names, fake phone numbers, fake Omang and passport numbers). Several rows are deliberately wrong or
unclear so you can see the preview report problems.
"""
import csv

from openpyxl import Workbook

from app.config import PROJECT_ROOT

HEADER = ["first_name", "surname", "phone", "id_number", "id_type", "employee_code"]
ROWS = [
    # first_name, surname, phone, id_number, id_type, employee_code      -> what the preview should say
    ["Anna", "Testerson", "71000001", "999900011", "omang", "T001"],        # new
    ["Ben", "Sample", "26771000002", "999900022", "", ""],                  # new + warning: type blank, taken as Omang
    ["Chipo", "Fakename", "+26771000003", "ab1234567", "passport", "T003"], # new passport; password will be 4567
    ["Dina", "Demo", "0026771000004", "123", "omang", ""],                  # warning: Omang "123" padded to 0123
    ["Emma", "Mock", "71 000 005", "zz99887766", "", ""],                   # new + warning: blank type, taken as passport
    ["Farai", "Placeholder", "71000006", "12AB34567", "omang", ""],         # ERROR: an Omang can't contain letters
    ["Gift", "Trial", "7100", "999900077", "omang", ""],                    # ERROR: phone too short
    ["Hope", "Duplicate", "71000008", "999900088", "omang", ""],            # ERROR: same name twice, no code
    ["hope", "duplicate", "71000009", "999900099", "omang", ""],            # ERROR: same name twice, no code
    ["Ian", "Landline", "31000010", "999900100", "omang", ""],              # warning: not a mobile number
    ["Jo", "Abroad", "+27821000011", "X1", "passport", "T011"],             # ERROR: passport needs 5 to 15 characters
    ["Kea", "Nophone", "", "999900122", "omang", ""],                       # warning: no phone
    ["Lebo", "Mixed", "71000013", "bp0012345", "omang", ""],                # ERROR: passport-looking number marked omang
]


def main() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Employees"
    ws.append(HEADER)
    for row in ROWS:
        ws.append(row)
    # Mimic Excel dropping leading zeros: store Dina's short Omang and Emma's phone as real numbers.
    ws["D5"] = 123          # Dina's Omang as a number
    ws["C6"] = 71000005     # Emma's phone as a number
    xlsx = PROJECT_ROOT / "sample_import.xlsx"
    wb.save(xlsx)

    csv_path = PROJECT_ROOT / "sample_import.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(ROWS)
    print(f"Created {xlsx.name} and {csv_path.name} (fake data only).")


if __name__ == "__main__":
    main()
