"""
EML parser - Parses .eml files, extracts body and saves attachments
"""
import email
from email import header
from email.message import Message
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from src.utils.logger import get_logger

logger = get_logger(__name__)


class EMLParser:
    """Parser for .eml email files"""
    
    def __init__(self, output_dir: str = "output"):
        """
        Initialize EML parser
        
        Args:
            output_dir: Directory to save attachments
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def parse(self, eml_path: str) -> Dict:
        """
        Parse an .eml file and extract content
        
        Args:
            eml_path: Path to the .eml file
            
        Returns:
            Dictionary containing parsed email data
        """
        eml_path = Path(eml_path)
        if not eml_path.exists():
            raise FileNotFoundError(f"EML file not found: {eml_path}")
        
        logger.info(f"Parsing EML file: {eml_path}")
        
        with open(eml_path, 'rb') as f:
            msg = email.message_from_bytes(f.read())
        
        # Extract email metadata
        email_data = {
            'subject': msg.get('Subject', ''),
            'from': msg.get('From', ''),
            'to': msg.get('To', ''),
            'date': msg.get('Date', ''),
            'body': self._extract_body(msg),
            'html_body': self._extract_html_body(msg),
            'attachments': []
        }
        
        # Extract attachments
        attachments = self._extract_attachments(msg, eml_path.stem)
        email_data['attachments'] = attachments
        
        logger.info(f"Extracted {len(attachments)} attachments")
        return email_data
    
    def _extract_body(self, msg: Message) -> str:
        """
        Extract email body text
        
        Args:
            msg: Email message object
            
        Returns:
            Email body as string
        """
        body = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    continue
                
                # Extract text content
                if content_type == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    except Exception as e:
                        logger.warning(f"Error decoding text/plain: {e}")
                elif content_type == "text/html" and not body:
                    try:
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as e:
                        logger.warning(f"Error decoding text/html: {e}")
        else:
            # Single part message
            try:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except Exception as e:
                logger.warning(f"Error decoding single part message: {e}")
        
        return body.strip()
    
    def _extract_html_body(self, msg: Message) -> str:
        """
        Extract the HTML body from the email for rich rendering.
        Returns the raw HTML string, or empty string if not available.
        """
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in content_disposition:
                    continue
                if content_type == "text/html":
                    try:
                        return part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as e:
                        logger.warning(f"Error decoding HTML body: {e}")
        else:
            if msg.get_content_type() == "text/html":
                try:
                    return msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.warning(f"Error decoding HTML body: {e}")
        return ""

    def _extract_attachments(self, msg: Message, email_name: str) -> List[Dict]:
        """
        Extract and save attachments from email
        
        Args:
            msg: Email message object
            email_name: Base name for attachment directory
            
        Returns:
            List of attachment metadata dictionaries
        """
        attachments = []
        
        if not msg.is_multipart():
            return attachments
        
        # Create subdirectory for this email's attachments
        attachment_dir = self.output_dir / "attachments" / email_name
        attachment_dir.mkdir(parents=True, exist_ok=True)
        
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", "")).lower()
            content_type = part.get_content_type().lower()
            filename = part.get_filename()
            
            # Determine if this part is an attachment
            is_attachment = False
            
            # Method 1: Explicit attachment in Content-Disposition
            if "attachment" in content_disposition:
                is_attachment = True
            
            # Method 2: Has filename and is not text/html/plain (likely an attachment)
            elif filename:
                # Skip if it's part of the email body structure
                if content_type not in ['text/plain', 'text/html', 'multipart/alternative', 'multipart/related', 'multipart/mixed']:
                    is_attachment = True
                # Also check if filename has a document extension
                elif any(ext in filename.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.jpg', '.jpeg', '.png', '.gif', '.zip', '.rar', '.txt', '.csv']):
                    is_attachment = True
            
            if is_attachment and filename:
                # Clean filename
                try:
                    decoded_filename = header.decode_header(filename)[0][0]
                    if isinstance(decoded_filename, bytes):
                        filename = decoded_filename.decode('utf-8', errors='ignore')
                    else:
                        filename = decoded_filename
                except Exception as e:
                    logger.warning(f"Error decoding filename {filename}: {e}")
                    # Use filename as-is if decoding fails
                    if isinstance(filename, bytes):
                        filename = filename.decode('utf-8', errors='ignore')
                
                filepath = attachment_dir / filename
                
                try:
                    # Save attachment
                    with open(filepath, 'wb') as f:
                        payload = part.get_payload(decode=True)
                        if payload:
                            f.write(payload)
                        else:
                            # Try without decode
                            payload = part.get_payload()
                            if isinstance(payload, bytes):
                                f.write(payload)
                            else:
                                logger.warning(f"Could not extract payload for {filename}")
                                continue
                    
                    attachments.append({
                        'filename': filename,
                        'filepath': str(filepath),
                        'content_type': part.get_content_type(),
                        'size': filepath.stat().st_size if filepath.exists() else 0
                    })
                    
                    logger.info(f"Saved attachment: {filename} (disposition: {content_disposition or 'none'})")
                except Exception as e:
                    logger.error(f"Error saving attachment {filename}: {e}")
        
        return attachments
