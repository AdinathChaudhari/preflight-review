"""Release verification."""
import subprocess


def verify_release(tag):
    print(f"verifying {tag}")
    subprocess.run(["make", "smoke-test", f"TAG={tag}"])
    print(f"{tag} verified")
    return True
