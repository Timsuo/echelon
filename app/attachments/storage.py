import hashlib
import os
import re
import stat
from pathlib import Path


def safe_filename(filename: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", filename).strip(" .")
    name = name.replace("..", "_")[:100].rstrip(" .") or "attachment"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CLOCK$",
                                        *[f"COM{i}" for i in range(10)],
                                        *[f"LPT{i}" for i in range(10)]}:
        name = "_" + name
    return name


class AttachmentStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self._check_links(self.root)

    @staticmethod
    def _check_links(path: Path) -> None:
        for component in (path, *path.parents):
            if component.is_symlink() or component.is_junction():
                raise PermissionError("Attachment path contains a link")
            if component.exists():
                info = component.lstat()
                if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PermissionError("Attachment path contains a reparse point")

    def path_for(self, attachment: dict) -> Path:
        for key in ("self_id", "group_id", "id"):
            if type(attachment[key]) is not int or attachment[key] <= 0:
                raise PermissionError("Invalid attachment identity")
        path = self.root / str(attachment["self_id"]) / str(attachment["group_id"]) / (
            f"{attachment['id']}-{safe_filename(attachment['filename'])}")
        self._check_links(path)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise PermissionError("Attachment path escapes storage")
        return path

    def prepare(self, attachment: dict) -> tuple[Path, Path]:
        final = self.path_for(attachment)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(final.name + ".part")
        self._check_links(temporary)
        if temporary.exists():
            temporary.unlink()  # Only this attachment's interrupted partial file.
        return temporary, final

    def validate(self, attachment: dict) -> Path:
        if attachment["download_status"] != "downloaded" or not attachment.get("local_path"):
            raise PermissionError("Attachment not downloaded")
        expected = self.path_for(attachment)
        supplied = Path(attachment["local_path"])
        if ".." in supplied.parts or not supplied.is_absolute() or supplied != expected:
            raise PermissionError("Attachment path does not match its database identity")
        self._check_links(supplied)
        info = supplied.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Attachment must be a regular non-linked file")
        return supplied

    def verify(self, attachment: dict) -> Path:
        path = self.validate(attachment)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not attachment.get("sha256") or digest != attachment["sha256"]:
            raise PermissionError("Attachment integrity check failed")
        return path

    def finish(self, temporary: Path, final: Path) -> None:
        self._check_links(temporary)
        self._check_links(final)
        os.replace(temporary, final)
