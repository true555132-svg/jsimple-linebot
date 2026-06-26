import importlib

def get_brand_profile(brand_key):
    """Load PROFILE from prompts/brands/<brand_key>.py. Returns None if not found."""
    if not brand_key:
        return None
    try:
        mod = importlib.import_module(f"prompts.brands.{brand_key}")
        return mod.PROFILE
    except (ImportError, AttributeError):
        return None
