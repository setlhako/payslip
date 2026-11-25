"""Re-encrypt every stored ID number with the newest key. Run: python -m app.scripts.reencrypt_ids

Used after `python -m app.scripts.generate_encryption_key --rotate`. Safe to run more than once. Only counts are printed.
It also re-computes every employee's ID blind index (`id_number_hash`), because that hash is keyed from the newest
encryption key and so changes whenever the key is rotated. Until this has run, duplicate-ID checks cannot see old rows.
"""
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Employee
from app.services import audit
from app.services.id_crypto import IdCipher, IdCryptoError, get_cipher


def reencrypt_all(db: Session, cipher: IdCipher) -> tuple[int, list[int]]:
    """Returns (how many were re-encrypted, employee ids whose value could not be read). Caller does NOT need to commit."""
    done, failed = 0, []
    for employee in db.scalars(select(Employee).where(Employee.id_number_encrypted.is_not(None))):
        try:
            employee.id_number_encrypted = cipher.rotate(employee.id_number_encrypted)
            employee.id_number_hash = cipher.blind_index(cipher.decrypt(employee.id_number_encrypted))   # in memory only
            done += 1
        except IdCryptoError:
            failed.append(employee.id)
    audit.record(db, "id_keys_rotated", details=f"reencrypted={done} failed={len(failed)}", commit=False)
    db.commit()
    return done, failed


def main() -> int:
    with SessionLocal() as db:
        done, failed = reencrypt_all(db, get_cipher())
    print(f"Re-encrypted {done} ID number(s) with the current key.")
    if failed:
        print(f"{len(failed)} could not be read with any known key (employee ids: {', '.join(map(str, failed[:20]))}).")
        print("Keep ID_ENCRYPTION_KEY_PREVIOUS as it is, and import those employees again to refill their IDs.")
        return 1
    print("All stored IDs now use the newest key. You can remove ID_ENCRYPTION_KEY_PREVIOUS from .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
