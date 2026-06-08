import sys
import os
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from backend.database.connection import SessionLocal
from backend.database.models import Crop, CropStage, CropStageRule
from backend.database.init import init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

# Categorized Crop List
CROP_CATEGORIES = {
    "Cereal": [
        "Sugarcane", "Paddy (Rice)", "Wheat", "Maize", "Sorghum (Jowar)", 
        "Bajra (Pearl Millet)", "Finger Millet (Ragi)", "Prosomillet (Kutki)", 
        "Small Millets", "Sweet Corn"
    ],
    "Pulse": [
        "Pigeon Pea (Tur/Arhar)", "Chickpea (Black Gram/Bengal Gram)", 
        "Moong (Green Gram)", "Urad (Black Gram)", "Lentil (Masoor)", 
        "Horse Gram (Kulthi)", "Moth Bean", "Cowpea", "Lablab Bean (Hyacinth Bean)", 
        "Green Peas", "Cluster Bean (Guar/Gawar)", "Dolichos Bean"
    ],
    "Vegetable": [
        "Tomato", "Onion", "Potato", "Brinjal (Eggplant)", "Okra (Bhindi)", 
        "Cabbage", "Cauliflower", "Capsicum/Bell Pepper (Open + Polyhouse)", 
        "Bottle Gourd", "Ridge Gourd", "Bitter Gourd", "Snake Gourd", 
        "Sponge Gourd", "Pumpkin", "Cucumber", "Pointed Gourd", 
        "Ash Gourd (Winter Melon)", "Ivy Gourd", "Fenugreek (Methi)", 
        "Spinach", "Amaranth (Rajgira)", "Coriander Leaves", "Cowpea (Vegetable)", 
        "Drumstick", "Beetroot", "Carrot", "Radish", "Turnip", "Beet Root", 
        "Sweet Potato", "Cassava (Tapioca)", "Yam (Kangar/Greater Yam)", 
        "Colocasia", "Lesser Yam", "Elephant Foot Yam"
    ],
    "Fruit": [
        "Mango", "Banana", "Orange/Citrus", "Sapota (Chikoo)", "Guava", 
        "Pomegranate", "Papaya", "Mosambi (Sweet Orange)", "Grapes", 
        "Sweet Lime (Kagzi)", "Lemon", "Strawberry", "Custard Apple (Sitaphal)", 
        "Ber", "Tamarind", "Aonla (Indian Gooseberry)", "Pineapple", "Fig", 
        "Karonda", "Jamun", "Jackfruit", "Wood Apple", "Charoli", "Coconut", 
        "Cashew", "Arecanut (Betel Nut/Supari)", "Watermelon", "Muskmelon", "Mulberry"
    ],
    "Spice": [
        "Turmeric (Haldi)", "Coriander", "Garlic", "Ginger", "Black Pepper", 
        "Clove", "Cinnamon", "Nutmeg", "Kokum", "Cardamom (Elaichi)"
    ],
    "Flower": [
        "Jasmine", "Tuberose", "Mogra (Jasmine variant)", "Marigold", 
        "Chrysanthemum", "Rose (Open + Polyhouse)", "Aster", "Gaillardia", 
        "Kagda (Local flower)", "Gerbera", "Carnation", "Gladiolus", 
        "Anthurium", "Orchids"
    ],
    "Medicinal": [
        "Musk Dana (Muskmallow)", "Aloe Vera", "Tulsi (Holy Basil)", "Stevia", 
        "Ashwagandha", "Brahmi", "Gloriosa", "Lemongrass", "Citronella", 
        "Java Citronella", "Patchouli", "Basil (Tulsi)", "Mint"
    ],
    "Cash Crop": [
        "Cotton", "Soybean", "Groundnut", "Sunflower", "Safflower", 
        "Sesame (Til)", "Linseed", "Mustard", "Niger", "Betel Leaves (Pan)", 
        "Tobacco"
    ]
}

# Standard stages for different crop groups
STAGES_BY_CATEGORY = {
    "Fruit": [
        {"name": "Vegetative Growth", "order": 1, "description": "Branching and leaf development phase."},
        {"name": "Flowering", "order": 2, "description": "Pollination and flower bud initiation phase."},
        {"name": "Fruiting/Development", "order": 3, "description": "Fruit setting and maturation phase."},
        {"name": "Harvesting", "order": 4, "description": "Harvesting phase."}
    ],
    "Flower": [
        {"name": "Bud Initiation", "order": 1, "description": "Formation of flower buds."},
        {"name": "Flowering/Plucking", "order": 2, "description": "Active bloom and plucking stage."},
        {"name": "Pruning/Rest", "order": 3, "description": "Dormancy and post-harvest trimming."}
    ],
    # Default for Cereal, Pulse, Vegetable, Spice, Medicinal, Cash Crop
    "Default": [
        {"name": "Sowing/Germination", "order": 1, "description": "Seed sowing, sprouting, and seedling emergence."},
        {"name": "Vegetative Growth", "order": 2, "description": "Leaf, stem, and root expansion."},
        {"name": "Flowering & Podding/Fruiting", "order": 3, "description": "Flowering, pollination, and fruit/pod setting."},
        {"name": "Maturity/Harvest", "order": 4, "description": "Maturation of grains/fruits and harvesting."}
    ]
}

# Standard weather alert rules by stage
RULES_BY_STAGE = {
    # 1. Sowing / Germination / Bud Initiation
    1: [
        {
            "name": "Excessive Rainfall (Sowing Risk)",
            "parameter": "precipitation",
            "operator": ">",
            "threshold": 25.0,
            "severity": "WARNING",
            "advisory": "Heavy rain expected. Delay sowing and clear drainage channels to prevent waterlogging."
        },
        {
            "name": "Low Temperature (Germination Delay)",
            "parameter": "temp_min",
            "operator": "<",
            "threshold": 12.0,
            "severity": "INFO",
            "advisory": "Cold weather may delay germination. Keep soil slightly moist."
        }
    ],
    
    # 2. Vegetative Growth / Active blooming
    2: [
        {
            "name": "High Wind Risk (Physical Damage)",
            "parameter": "wind_speed_max",
            "operator": ">",
            "threshold": 12.0,
            "severity": "WARNING",
            "advisory": "Strong winds expected. Support young plants and delay pesticide spraying."
        },
        {
            "name": "Extreme Heat Stress",
            "parameter": "temp_max",
            "operator": ">",
            "threshold": 38.0,
            "severity": "WARNING",
            "advisory": "Hot weather. Water crops in the morning or evening to protect them."
        }
    ],
    
    # 3. Flowering & Fruiting / Fruit Development
    3: [
        {
            "name": "Extreme Temperature (Flower Drop)",
            "parameter": "temp_max",
            "operator": ">",
            "threshold": 35.0,
            "severity": "CRITICAL",
            "advisory": "Extreme heat can cause flower drop. Water crops immediately to protect them."
        },
        {
            "name": "High Humidity (Fungal Disease Risk)",
            "parameter": "humidity_avg",
            "operator": ">",
            "threshold": 85.0,
            "severity": "WARNING",
            "advisory": "Damp weather increases disease risk. Check crops for pests and apply organic spray."
        },
        {
            "name": "Heavy Rainfall (Pollination Disruption)",
            "parameter": "precipitation",
            "operator": ">",
            "threshold": 15.0,
            "severity": "WARNING",
            "advisory": "Rain expected. Delay manual pollination and spray applications."
        }
    ],
    
    # 4. Maturity / Harvest / Dormancy
    4: [
        {
            "name": "Pre-Harvest Rainfall Damage",
            "parameter": "precipitation",
            "operator": ">",
            "threshold": 8.0,
            "severity": "CRITICAL",
            "advisory": "Rain during harvest can spoil crops. Delay harvest and move harvested crops to safe shelter."
        },
        {
            "name": "Frost Risk (Cold Injury)",
            "parameter": "temp_min",
            "operator": "<",
            "threshold": 4.0,
            "severity": "CRITICAL",
            "advisory": "Frost warning! Water fields or light small fires to protect crops from extreme cold."
        }
    ]
}

def seed_database():
    # Initialize database tables if not already present
    init_db()
    
    session: Session = SessionLocal()
    try:
        logger.info("Truncating crops, crop_stages, and crop_stage_rules tables...")
        session.execute(text("TRUNCATE TABLE crops, crop_stages, crop_stage_rules RESTART IDENTITY CASCADE;"))
        session.commit()
        
        logger.info("Seeding crops, stages, and advisory rules...")
        
        crop_count = 0
        stage_count = 0
        rule_count = 0
        
        for category, crop_names in CROP_CATEGORIES.items():
            for name in crop_names:
                # 1. Insert Crop
                crop = Crop(name=name, category=category)
                session.add(crop)
                session.flush()  # populate crop.id
                crop_count += 1
                
                # 2. Select stages pattern
                stages_to_add = STAGES_BY_CATEGORY.get(category, STAGES_BY_CATEGORY["Default"])
                
                # 3. Insert Stages and their Rules
                for stage_data in stages_to_add:
                    stage = CropStage(
                        crop_id=crop.id,
                        stage_name=stage_data["name"],
                        stage_order=stage_data["order"],
                        description=stage_data["description"]
                    )
                    session.add(stage)
                    session.flush()  # populate stage.id
                    stage_count += 1
                    
                    # Map standard rules based on the stage order (1-4)
                    rules_to_add = RULES_BY_STAGE.get(stage_data["order"], [])
                    for rule_data in rules_to_add:
                        rule = CropStageRule(
                            crop_stage_id=stage.id,
                            rule_name=rule_data["name"],
                            parameter=rule_data["parameter"],
                            operator=rule_data["operator"],
                            threshold_value=rule_data["threshold"],
                            severity=rule_data["severity"],
                            advisory_text=rule_data["advisory"]
                        )
                        session.add(rule)
                        rule_count += 1
                        
        session.commit()
        logger.info(f"Seeding completed successfully! Seeded {crop_count} crops, {stage_count} stages, and {rule_count} rules.")
        
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to seed database: {e}", exc_info=True)
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    seed_database()
