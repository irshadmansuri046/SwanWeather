import logging
import operator
from decimal import Decimal
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select
from backend.database.models import WeatherDaily, CropStageRule, CropStage, Crop

logger = logging.getLogger(__name__)

# Operator mapping for string-based comparisons
OPERATOR_MAP = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le
}

class AdvisoryEngine:
    @staticmethod
    def evaluate_rules(db: Session, latitude: float, longitude: float, crop_stage_pairs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Evaluates the 10-day daily weather forecasts against the rules for specific crop stages.
        
        Args:
            db (Session): Database session.
            latitude (float): Location latitude.
            longitude (float): Location longitude.
            crop_stage_pairs (list): List of dicts, e.g. [{"crop_id": 1, "stage_id": 2}, ...]
            
        Returns:
            list: List of generated advisories grouped by date, crop, and stage.
        """
        # Round to nearest 0.25 grid point to match GRIB forecasts
        lat_grid = round(latitude * 4) / 4
        lon_grid = round(longitude * 4) / 4
        
        # 1. Fetch 10-day daily weather forecasts
        stmt_weather = select(WeatherDaily).where(
            WeatherDaily.latitude == lat_grid,
            WeatherDaily.longitude == lon_grid
        ).order_by(WeatherDaily.forecast_date.asc())
        daily_forecasts = db.execute(stmt_weather).scalars().all()
        
        if not daily_forecasts:
            logger.warning(f"No weather daily forecast found for ({lat_grid}, {lon_grid}) in database.")
            return []
            
        advisories = []
        
        # 2. Iterate over the requested crop-stage pairs
        for pair in crop_stage_pairs:
            crop_id = pair.get("crop_id")
            stage_id = pair.get("stage_id")
            
            # Fetch Crop and Stage metadata
            crop = db.get(Crop, crop_id)
            stage = db.get(CropStage, stage_id)
            if not crop or not stage:
                logger.warning(f"Crop ID {crop_id} or Stage ID {stage_id} not found.")
                continue
                
            # Fetch all weather rules defined for this stage
            stmt_rules = select(CropStageRule).where(CropStageRule.crop_stage_id == stage_id)
            rules = db.execute(stmt_rules).scalars().all()
            
            if not rules:
                continue
                
            # 3. Check each day's forecast against the rules
            for day in daily_forecasts:
                day_alerts = []
                for rule in rules:
                    # Get actual weather value (temp_min, temp_max, precipitation, humidity_avg, wind_speed_max)
                    actual_val = getattr(day, rule.parameter, None)
                    if actual_val is None:
                        continue
                        
                    # Evaluate threshold comparison
                    op_func = OPERATOR_MAP.get(rule.operator)
                    if op_func and op_func(Decimal(str(actual_val)), Decimal(str(rule.threshold_value))):
                        day_alerts.append({
                            "rule_id": rule.id,
                            "rule_name": rule.rule_name,
                            "parameter": rule.parameter,
                            "actual_value": float(actual_val),
                            "threshold_value": float(rule.threshold_value),
                            "operator": rule.operator,
                            "severity": rule.severity,
                            "advisory_text": rule.advisory_text
                        })
                
                if day_alerts:
                    advisories.append({
                        "date": day.forecast_date.isoformat(),
                        "crop_name": crop.name,
                        "stage_name": stage.stage_name,
                        "alerts": day_alerts
                    })
                    
        return advisories
