/**
 * Display names for the maintenance types the backend seeds (its "system"
 * types). Their names are stored in English and double as the wiki-link key,
 * so the stored name stays untouched and only what the user reads is
 * translated; a custom type, or a system type someone renamed, is shown as
 * stored.
 */
type TFunction = (key: string, options?: Record<string, unknown>) => string;

const SYSTEM_TYPE_KEYS: Record<string, string> = {
  'Clean Carbon Rods': 'cleanCarbonRods',
  'Lubricate Carbon Rods': 'lubricateCarbonRods',
  'Lubricate Steel Rods': 'lubricateSteelRods',
  'Clean Steel Rods': 'cleanSteelRods',
  'Lubricate Linear Rails': 'lubricateRails',
  'Clean Linear Rails': 'cleanLinearRails',
  'Clean Nozzle/Hotend': 'cleanNozzle',
  'Check Belt Tension': 'checkBelts',
  'Clean Build Plate': 'cleanBuildPlate',
  'Check Extruder Gears': 'checkExtruder',
  'Check Cooling Fans': 'checkCooling',
  'General Inspection': 'generalInspection',
  'Check PTFE Tube': 'checkPtfeTube',
  'Replace HEPA Filter': 'replaceHepaFilter',
  'Replace Carbon Filter': 'replaceCarbonFilter',
  'Lubricate Left Nozzle Rail': 'lubricateLeftNozzleRail',
  'Printer Calibration': 'printerCalibration',
  'Vision Encoder Calibration': 'visionEncoderCalibration',
};

export function maintenanceTypeLabel(name: string, t: TFunction): string {
  const key = SYSTEM_TYPE_KEYS[name];
  return key ? t(`maintenance.types.${key}`, { defaultValue: name }) : name;
}
