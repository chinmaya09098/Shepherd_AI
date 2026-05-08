"""
Azure Content Understanding (OCR) logic for extracting text from documents
"""
import os
import requests
from typing import Optional, Dict
from pathlib import Path
from src.config import Config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Try to import pandas for Excel support
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    logger.warning("pandas not available. Excel file support will be limited.")


class ContentUnderstandingExtractor:
    """Extractor for Azure Content Understanding (OCR) service"""
    
    def __init__(self):
        """Initialize Content Understanding extractor"""
        self.endpoint = Config.AZURE_CONTENT_UNDERSTANDING_ENDPOINT
        self.api_key = Config.AZURE_CONTENT_UNDERSTANDING_KEY
        self.api_version = Config.AZURE_CONTENT_UNDERSTANDING_API_VERSION
        
        if not self.endpoint or not self.api_key:
            raise ValueError("Azure Content Understanding endpoint and API key must be configured")
    
    def extract_text(self, file_path: str):
        """
        Extract text from a document using Azure Content Understanding or Excel parsing.

        Returns:
            Tuple of (text, confidence) where confidence is 0.0-1.0 (None for Excel files),
            or None if extraction fails.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return None

        logger.info(f"Extracting text from: {file_path}")

        # Excel files are parsed locally via pandas — no OCR, no confidence score, no raw result
        if file_path.suffix.lower() in ['.xlsx', '.xls']:
            text = self._extract_text_from_excel(file_path)
            return (text, None, None) if text else None

        # PDFs and other documents go through Azure Document Intelligence
        try:
            with open(file_path, 'rb') as f:
                file_content = f.read()

            url = f"{self.endpoint}/formrecognizer/documentModels/prebuilt-read:analyze"
            headers = {
                "Ocp-Apim-Subscription-Key": self.api_key,
                "Content-Type": "application/octet-stream"
            }
            params = {"api-version": self.api_version}

            response = requests.post(url, headers=headers, params=params, data=file_content)
            response.raise_for_status()

            operation_location = response.headers.get("Operation-Location")
            if not operation_location:
                logger.error("No Operation-Location header in response")
                return None

            result = self._poll_for_results(operation_location)

            if result:
                text, confidence = self._extract_text_from_result(result)
                confidence_str = f"{confidence:.3f}" if confidence is not None else "N/A"
                logger.info(f"Successfully extracted {len(text)} characters, confidence: {confidence_str}")
                return (text, confidence, result)
            else:
                logger.error("Failed to get extraction results")
                return None

        except Exception as e:
            logger.error(f"Error extracting text: {e}")
            return None
    
    def _poll_for_results(self, operation_location: str, max_attempts: int = 30) -> Optional[Dict]:
        """
        Poll for analysis results
        
        Args:
            operation_location: URL to poll for results
            max_attempts: Maximum number of polling attempts
            
        Returns:
            Result dictionary or None
        """
        import time
        
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key
        }
        
        for attempt in range(max_attempts):
            try:
                response = requests.get(operation_location, headers=headers)
                response.raise_for_status()
                
                result = response.json()
                status = result.get("status", "")
                
                if status == "succeeded":
                    return result
                elif status == "failed":
                    logger.error(f"Analysis failed: {result.get('error', {})}")
                    return None
                else:
                    # Still processing, wait and retry
                    time.sleep(2)
                    
            except Exception as e:
                logger.warning(f"Polling attempt {attempt + 1} failed: {e}")
                time.sleep(2)
        
        logger.error("Max polling attempts reached")
        return None
    
    def _extract_text_from_excel(self, file_path: Path) -> Optional[str]:
        """
        Extract text from Excel file using pandas
        
        Args:
            file_path: Path to Excel file
            
        Returns:
            Extracted text as string or None if extraction fails
        """
        if not PANDAS_AVAILABLE:
            logger.error("pandas is not installed. Cannot read Excel files. Please install: pip install pandas openpyxl")
            return None
        
        try:
            logger.info(f"Reading Excel file: {file_path}")
            
            # Read all sheets from Excel file
            excel_file = pd.ExcelFile(file_path)
            text_parts = []
            
            # Process each sheet
            for sheet_name in excel_file.sheet_names:
                logger.info(f"Processing sheet: {sheet_name}")
                df = pd.read_excel(excel_file, sheet_name=sheet_name)
                
                # Add sheet header
                text_parts.append(f"\n=== Sheet: {sheet_name} ===\n")
                
                # Convert DataFrame to text representation
                # Replace NaN with empty strings for better readability
                df = df.fillna("")
                
                # Get column names
                columns = list(df.columns)
                text_parts.append(f"Columns: {', '.join([str(col) for col in columns])}\n")
                
                # Convert each row to a readable format
                for idx, row in df.iterrows():
                    row_text = []
                    for col in columns:
                        value = row[col]
                        if value != "" and pd.notna(value):
                            row_text.append(f"{col}: {value}")
                    
                    if row_text:
                        text_parts.append(f"Row {idx + 1}: {' | '.join(row_text)}")
                
                text_parts.append("")  # Add blank line between sheets
            
            extracted_text = "\n".join(text_parts)
            logger.info(f"Successfully extracted {len(extracted_text)} characters from Excel file")
            return extracted_text
            
        except Exception as e:
            logger.error(f"Error extracting text from Excel file: {e}", exc_info=True)
            return None
    
    def _extract_text_from_result(self, result: Dict):
        """
        Extract text and average word confidence from analysis result.

        Returns:
            Tuple of (text, confidence) where confidence is the average across all words.
        """
        text_parts = []
        all_confidences = []

        pages = result.get("analyzeResult", {}).get("pages", [])
        for page in pages:
            for line in page.get("lines", []):
                content = line.get("content", "")
                if content:
                    text_parts.append(content)
            for word in page.get("words", []):
                score = word.get("confidence")
                if score is not None:
                    all_confidences.append(score)

        text = "\n".join(text_parts)
        confidence = round(sum(all_confidences) / len(all_confidences), 3) if all_confidences else None
        return (text, confidence)
