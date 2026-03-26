#!/bin/bash
curl -s -X POST http://localhost:5001/arrivy-webhook \
  -H "Content-Type: application/json" \
  -d '{"EVENT_TYPE":"TASK_UPDATED","OBJECT_EXTERNAL_ID":"29905","OBJECT_TEMPLATE_ID":5395407346073600}'
echo ""
