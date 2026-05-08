"""Pydantic schemas for shipment data validation and serialization."""
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class ShipmentAddress(BaseModel):
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = Field(None, alias="zipCode")
    country: Optional[str] = None

    class Config:
        populate_by_name = True


class LocationInfo(BaseModel):
    """Location with name and address only. Contacts live in NiceToHaveFields."""
    name: Optional[str] = None
    address: Optional[ShipmentAddress] = None


class ContactInfo(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class ShipmentItem(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    weight: Optional[float] = None
    pieces: Optional[float] = None
    pallets: Optional[float] = None
    dimensions: Optional[str] = None


class RequiredFields(BaseModel):
    customer_name: Optional[str] = Field(None, alias="customerName")
    pickup_location: Optional[LocationInfo] = Field(None, alias="pickupLocation")
    drop_location: Optional[LocationInfo] = Field(None, alias="dropLocation")
    pickup_date: Optional[datetime] = Field(None, alias="pickupDate")
    pickup_window: Optional[str] = Field(None, alias="pickupWindow")
    delivery_date: Optional[datetime] = Field(None, alias="deliveryDate")
    equipment_mode: Optional[str] = Field(None, alias="equipmentMode")
    items: List[ShipmentItem] = Field(default_factory=list)
    total_weight: Optional[float] = Field(None, alias="totalWeight")
    accessorials: List[str] = Field(default_factory=list)

    class Config:
        populate_by_name = True
        json_encoders = {datetime: lambda v: v.isoformat() if v else None}


class NiceToHaveFields(BaseModel):
    extraction_confidence: Optional[float] = Field(None, alias="extractionConfidence")
    pickup_contact: Optional[ContactInfo] = Field(None, alias="pickupContact")
    drop_contact: Optional[ContactInfo] = Field(None, alias="dropContact")
    reference_numbers: List[str] = Field(default_factory=list, alias="referenceNumbers")
    special_instructions: Optional[str] = Field(None, alias="specialInstructions")
    temperature_requirements: Optional[str] = Field(None, alias="temperatureRequirements")
    declared_value: Optional[str] = Field(None, alias="declaredValue")
    notes: Optional[str] = None
    carrier: Optional[str] = None
    shipment_id: Optional[str] = Field(None, alias="shipmentId")
    order_number: Optional[str] = Field(None, alias="orderNumber")
    purchase_order: Optional[str] = Field(None, alias="purchaseOrder")
    tracking_number: Optional[str] = Field(None, alias="trackingNumber")

    class Config:
        populate_by_name = True


class Shipment(BaseModel):
    email_type: str = Field("shipment_tender", alias="emailType")
    missing_required_fields: List[str] = Field(default_factory=list, alias="missingRequiredFields")
    required_fields: RequiredFields = Field(default_factory=RequiredFields, alias="requiredFields")
    nice_to_have_fields: NiceToHaveFields = Field(default_factory=NiceToHaveFields, alias="niceToHaveFields")

    class Config:
        populate_by_name = True
        json_encoders = {datetime: lambda v: v.isoformat() if v else None}
