from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey, BigInteger
from sqlalchemy.orm import relationship
from app.database import Base


class Exchange(Base):
    __tablename__ = "exchanges"
    exchange_id = Column(Integer, primary_key=True)
    code = Column(String(10), nullable=False, unique=True)
    name = Column(String(100), nullable=False)
    country = Column(String(10), nullable=False, default="IN")


class InstrumentType(Base):
    __tablename__ = "instrument_types"
    instrument_type_id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False, unique=True)
    asset_class = Column(String(50), nullable=False)
    tax_category = Column(String(50), nullable=False)


class Instrument(Base):
    __tablename__ = "instruments"
    instrument_id = Column(Integer, primary_key=True)
    isin = Column(String(12), unique=True)
    name = Column(String(200), nullable=False)
    instrument_type_id = Column(Integer, ForeignKey("instrument_types.instrument_type_id"), nullable=False)
    primary_exchange_id = Column(Integer, ForeignKey("exchanges.exchange_id"))
    is_active = Column(Boolean, nullable=False, default=True)
    source = Column(String(20), nullable=False, default="SERVER")  # 'SERVER', 'MANUAL'
    created_at = Column(Text, nullable=False)
    updated_at = Column(Text, nullable=False)

    instrument_type = relationship("InstrumentType")
    exchange = relationship("Exchange")


class InstrumentEquity(Base):
    __tablename__ = "instrument_equity"
    instrument_id = Column(Integer, ForeignKey("instruments.instrument_id"), primary_key=True)
    nse_symbol = Column(String(20))
    bse_code = Column(String(10))
    face_value_paise = Column(Integer)
    sector = Column(String(100))
    industry = Column(String(100))


class InstrumentMF(Base):
    __tablename__ = "instrument_mf"
    instrument_id = Column(Integer, ForeignKey("instruments.instrument_id"), primary_key=True)
    amfi_code = Column(String(20))
    scheme_type = Column(String(50))   # 'EQUITY', 'DEBT', 'HYBRID', 'ELSS', etc.
    fund_house = Column(String(100))
    plan = Column(String(20))          # 'DIRECT', 'REGULAR'
    option = Column(String(20))        # 'GROWTH', 'IDCW'


class DailyPrice(Base):
    __tablename__ = "daily_prices"
    price_id = Column(BigInteger, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.instrument_id"), nullable=False)
    trade_date = Column(Text, nullable=False)
    open_price_paise = Column(Integer)
    high_price_paise = Column(Integer)
    low_price_paise = Column(Integer)
    close_price_paise = Column(Integer, nullable=False)
    volume = Column(BigInteger)
    source = Column(String(20), nullable=False)  # 'NSE', 'BSE', 'MCX'


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"
    holiday_date = Column(Text, primary_key=True)
    description = Column(Text)
    exchange_id = Column(Integer, ForeignKey("exchanges.exchange_id"))
