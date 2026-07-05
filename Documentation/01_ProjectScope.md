# 01_ProjectScope.md

# UrbanAir AI – AI-Powered Urban Air Quality Intelligence for Delhi NCR

## Project Overview

UrbanAir AI is an AI-powered geospatial decision intelligence platform designed for **Delhi NCR**. The platform helps city administrators move beyond passive air quality monitoring by providing predictive analytics, pollution source attribution, enforcement recommendations, and citizen health advisories.

Instead of only displaying AQI values, UrbanAir AI transforms environmental data into actionable intelligence that enables proactive intervention.

---

# Problem Statement

Current air quality monitoring systems provide AQI readings but do not answer critical questions such as:

- Why is pollution increasing?
- Which areas will become pollution hotspots tomorrow?
- What are the major pollution sources?
- Where should enforcement teams be deployed first?
- Which citizens are most vulnerable?

As a result, city administrations respond reactively rather than proactively.

---

# Proposed Solution

UrbanAir AI integrates air quality, weather, satellite imagery, traffic, land-use, and demographic data to build an AI-powered decision support platform for Delhi NCR.

The system provides:

- Hyperlocal AQI Forecasting
- Pollution Source Attribution
- Enforcement Intelligence
- Citizen Health Advisory
- AI-powered Chat Assistant
- What-if Simulation Engine (Stretch Feature)

---

# Prototype Scope

**Study Area:** Delhi NCR

The prototype will demonstrate all functionalities using Delhi NCR because of the availability of reliable public datasets and its relevance to India's urban air quality challenges.

The architecture is designed to be scalable and can later be extended to any Indian smart city.

---

# Primary Users

- Delhi Pollution Control Committee (DPCC)
- Municipal Corporation of Delhi (MCD)

---

# Secondary Users

- Citizens
- Hospitals
- Public Health Departments
- Urban Planners
- Environmental Researchers

---

# Objectives

The project aims to:

- Predict AQI for the next 24, 48, and 72 hours.
- Identify major pollution sources using AI.
- Detect pollution hotspots.
- Recommend enforcement priorities.
- Generate ward-level health advisories.
- Assist officials using a natural language AI assistant.
- Support data-driven environmental decision making.

---

# Core Features

## 1. Live AQI Dashboard

- Delhi NCR interactive map
- AQI monitoring stations
- Current AQI
- Heatmap visualization
- Historical trend charts

---

## 2. Hyperlocal AQI Forecast

- 24-hour forecast
- 48-hour forecast
- 72-hour forecast
- Ward/Station-level predictions

---

## 3. Pollution Source Attribution

Estimate pollution contribution from:

- Traffic
- Construction
- Industries
- Waste Burning

---

## 4. Enforcement Intelligence

Provide AI-generated recommendations such as:

- High-priority inspection locations
- Recommended actions
- Reasoning behind recommendations

---

## 5. Citizen Health Advisory

Generate:

- Risk levels
- Health recommendations
- Vulnerable population alerts
- Ward-wise advisories

---

## 6. AI Chat Assistant

Allow officials to ask questions such as:

- Why is AQI increasing?
- Which ward is most polluted?
- What action should be taken today?
- Which pollution source is dominant?

---

## 7. What-if Simulation Engine (Stretch Goal)

Simulate environmental interventions such as:

- Reduce construction activity
- Restrict heavy vehicles
- Increase green cover
- Reduce industrial emissions

Estimate:

- AQI improvement
- Population benefited
- Expected pollution reduction

---

# Success Workflow

```text
08:00 AM

↓

Officials open UrbanAir AI Dashboard

↓

AI collects latest environmental data

↓

Forecast Engine predicts AQI

↓

Source Attribution Engine identifies pollution sources

↓

Enforcement Intelligence prioritizes hotspot locations

↓

Health Advisory Engine generates citizen recommendations

↓

Officials deploy inspection teams

↓

Citizens receive advisories

↓

Proactive intervention reduces pollution impact
```

---

# Expected Benefits

## Government

- Faster decision making
- Better resource allocation
- Evidence-based enforcement
- Reduced response time

## Citizens

- Early health alerts
- Personalized recommendations
- Increased awareness

## Environment

- Improved monitoring
- Pollution hotspot identification
- Better intervention planning

---

# Technology Stack

## Frontend

- React
- Tailwind CSS
- Leaflet
- Recharts

## Backend

- FastAPI

## AI / ML

- Python
- Scikit-learn
- XGBoost
- LightGBM

## LLM

- Gemini API

## GIS

- GeoPandas
- OpenStreetMap
- PostGIS (Optional)

## Database

- PostgreSQL
- Redis (Optional)

---

# Deliverables

- Working Prototype
- Interactive Dashboard
- AI Models
- Architecture Diagrams
- Presentation Deck
- Demo Video
- Documentation
- GitHub Repository

---

# Team

**Project:** UrbanAir AI

**Team Members**

- Sushma Shukla
- Upma Shukla

---

# Project Vision

UrbanAir AI aims to transform air quality monitoring into an intelligent decision support system that empowers city administrators to predict pollution, identify its causes, prioritize interventions, and protect public health through AI-driven insights.
