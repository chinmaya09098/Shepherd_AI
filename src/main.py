"""
Main orchestration script for Shepherd AI POC
"""
import json
import sys
from pathlib import Path
from typing import List, Optional

# Add backend directory to Python path for imports
backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from src.config import Config
from src.readers.eml_parser import EMLParser
from src.extractors.content_understanding import ContentUnderstandingExtractor
from src.extractors.openai_agent import OpenAIAgent
from src.models.shipment import Shipment
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ShepherdAIProcessor:
    """Main processor for email and document extraction"""
    
    def __init__(self):
        """Initialize processor with required components"""
        if not Config.validate():
            raise ValueError("Configuration validation failed. Please check your .env file.")
        
        self.eml_parser = EMLParser(output_dir=Config.OUTPUT_DIR)
        self.content_extractor = ContentUnderstandingExtractor()
        self.openai_agent = OpenAIAgent()
    
    # File types that are never shipping documents — inline email images, logos, signatures
    _IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp', '.ico'}

    def process_email(self, eml_path: str) -> List[Shipment]:
        """
        Process a single email file and extract shipment data from each attachment.

        Two-pass approach:
          Pass 1 — Extract envelope context from email body + ALL attachment texts combined.
                   This ensures location/routing data in one attachment is available when
                   processing sibling attachments that don't repeat it.
          Pass 2 — Extract per-attachment shipment data using the envelope as read-only context.

        Returns:
            List of extracted Shipment objects (one per processable attachment)
        """
        logger.info(f"Processing email: {eml_path}")
        shipments = []

        try:
            email_data = self.eml_parser.parse(eml_path)
            logger.info(f"Email subject: {email_data['subject']}")

            # Filter out inline image attachments (logos, signatures, etc.)
            raw_attachments = email_data['attachments']
            attachments = [
                a for a in raw_attachments
                if Path(a['filename']).suffix.lower() not in self._IMAGE_EXTENSIONS
            ]
            filtered = len(raw_attachments) - len(attachments)
            logger.info(f"Found {len(attachments)} processable attachments ({filtered} image(s) filtered)")

            # Pre-extract text from all attachments so we can build the envelope
            attachment_data = []
            for attachment in attachments:
                text = self.content_extractor.extract_text(attachment['filepath'])
                if text:
                    attachment_data.append({'attachment': attachment, 'text': text})
                else:
                    logger.warning(f"No text extracted from {attachment['filename']}, skipping")

            # Pass 1: envelope from email body + all attachment texts combined
            all_attachment_texts = "\n\n---\n\n".join(
                f"[{d['attachment']['filename']}]\n{d['text']}" for d in attachment_data
            )
            envelope = self.openai_agent.extract_email_envelope(
                email_body=email_data['body'],
                all_attachment_texts=all_attachment_texts
            )
            logger.info("Email envelope extracted")

            # Pass 2: per-attachment extraction using envelope as read-only context
            for idx, data in enumerate(attachment_data, 1):
                attachment = data['attachment']
                logger.info(f"Processing attachment {idx}/{len(attachment_data)}: {attachment['filename']}")

                shipment = self.openai_agent.extract_shipment_data(
                    attachment_text=data['text'],
                    envelope=envelope
                )

                if shipment:
                    self._save_results(eml_path, shipment, attachment['filename'], idx)
                    shipments.append(shipment)
                    logger.info(f"Successfully extracted shipment data from {attachment['filename']}")
                else:
                    logger.warning(f"Failed to extract shipment data from {attachment['filename']}")

            logger.info(f"Processed {len(shipments)} shipments from {len(attachment_data)} attachments")
            return shipments

        except Exception as e:
            logger.error(f"Error processing email {eml_path}: {e}")
            return []
    
    def process_directory(self, directory: str) -> List[Shipment]:
        """
        Process all .eml files in a directory
        
        Args:
            directory: Directory containing .eml files
            
        Returns:
            List of extracted Shipment objects
        """
        directory = Path(directory)
        if not directory.exists():
            logger.error(f"Directory not found: {directory}")
            return []
        
        eml_files = list(directory.glob("*.eml"))
        logger.info(f"Found {len(eml_files)} EML files")
        
        shipments = []
        for eml_file in eml_files:
            email_shipments = self.process_email(str(eml_file))
            shipments.extend(email_shipments)
        
        return shipments
    
    def _save_results(self, eml_path: str, shipment: Shipment, attachment_filename: str = None, attachment_index: int = None):
        """
        Save extraction results to JSON file
        
        Args:
            eml_path: Path to original email file
            shipment: Extracted shipment data
            attachment_filename: Optional attachment filename for unique naming
            attachment_index: Optional attachment index for unique naming
        """
        output_dir = Path(Config.OUTPUT_DIR) / "results"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        eml_name = Path(eml_path).stem
        
        # Create unique filename based on attachment
        if attachment_filename:
            # Use attachment filename (without extension) for unique identification
            attachment_name = Path(attachment_filename).stem
            output_file = output_dir / f"{eml_name}_{attachment_name}_shipment.json"
        elif attachment_index:
            # Fallback to index if filename not available
            output_file = output_dir / f"{eml_name}_attachment_{attachment_index}_shipment.json"
        else:
            # Original naming if no attachment info
            output_file = output_dir / f"{eml_name}_shipment.json"
        
        # Use model_dump_json() which handles datetime serialization automatically
        json_content = shipment.model_dump_json(by_alias=True, exclude_none=True, indent=2)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(json_content)
        
        logger.info(f"Saved results to: {output_file}")


def main():
    """Main entry point"""
    try:
        processor = ShepherdAIProcessor()
        
        # Process emails from configured directory
        email_dir = Path(Config.EMAIL_EXAMPLES_DIR)
        if email_dir.exists():
            shipments = processor.process_directory(str(email_dir))
            logger.info(f"Processed {len(shipments)} emails successfully")
        else:
            logger.warning(f"Email directory not found: {email_dir}")
            logger.info("Please place .eml files in the Load_Tender_Email_Examples directory")
            
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
