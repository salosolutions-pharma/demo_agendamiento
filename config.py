import os
from dotenv import load_dotenv

# Cargar variables de entorno desde .env
load_dotenv()

class Config:
    """Configuración centralizada para todos los servicios"""
    
    # Azure Speech Service
    AZURE_SUBSCRIPTION_KEY = os.getenv("AZURE_SUBSCRIPTION_KEY")
    AZURE_REGION = os.getenv("AZURE_REGION", "eastus")
    
    # Twilio
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_NUMBER = os.getenv("TWILIO_NUMBER")
    
    # Telnyx
    TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
    TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
    TELNYX_NUMBER = os.getenv("TELNYX_NUMBER")
    
    # Números de prueba
    TEST_NUMBER = os.getenv("TEST_NUMBER", "+573137727034")
    
    # URL base
    BASE_URL = os.getenv("BASE_URL", "https://call-api-283783157844.us-central1.run.app")
    
    # Proveedores disponibles
    VOICE_PROVIDERS = ["azure"]
    CALL_PROVIDERS = ["twilio", "telnyx"]
    
    # Configuración por defecto
    DEFAULT_VOICE_PROVIDER = "azure"
    DEFAULT_CALL_PROVIDER = "twilio"
    
    @classmethod
    def validate_config(cls):
        """Valida que todas las variables necesarias estén configuradas"""
        required_vars = [
            'AZURE_SUBSCRIPTION_KEY',
            'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_NUMBER',
            'TELNYX_API_KEY', 'TELNYX_CONNECTION_ID', 'TELNYX_NUMBER'
        ]
        
        missing = []
        for var in required_vars:
            if not getattr(cls, var):
                missing.append(var)
        
        if missing:
            raise ValueError(f"Faltan variables de entorno: {', '.join(missing)}")
        
        return True

config = Config()