# Database schema

Generated from the models by `python -m tools.schema_doc` - do not edit by hand.
Quantities are stored as integer thousandths (`Quantity`), money as integer 1/10,000 GBP (`Money`),
times in UTC. See docs/STOCK_LEDGER.md for how stock is recorded and docs/ARCHITECTURE.md for the
layers that use these tables.

Protected tables (database triggers, see `wms.db.engine`):

* Append-only (no UPDATE, no DELETE): `stock_movements`, `audit_log`, `movement_reversals`
* Never deleted; a posted line never changes: `delivery_notes`, `goods_issues`, `stock_returns`, `production_orders`, `stock_adjustments`, `stock_transfers`, `stock_counts`, `delivery_note_lines`, `goods_issue_lines`, `return_lines`, `production_lines`, `stock_count_lines`

## attachments

| Column | Type | Null | Key / reference |
|---|---|---|---|
| entity_type | String(30) | no |  |
| entity_id | Integer | no |  |
| kind | String(20) | no |  |
| original_name | String(260) | no |  |
| stored_name | String(300) | no | UNIQUE |
| mime_type | String(100) | no |  |
| size_bytes | Integer | no |  |
| sha256 | String(64) | no | indexed |
| is_primary | Boolean | no |  |
| caption | String(200) | yes |  |
| uploaded_by_id | Integer | no | FK users.id, indexed |
| uploaded_at | UTCDateTime | no |  |
| is_deleted | Boolean | no |  |
| deleted_at | UTCDateTime | yes |  |
| deleted_by_id | Integer | yes | FK users.id, indexed |
| id | Integer | no | PK |

* INDEX ix_attachments_entity (entity_type, entity_id)

## audit_log

| Column | Type | Null | Key / reference |
|---|---|---|---|
| occurred_at | UTCDateTime | no | indexed |
| user_id | Integer | yes | FK users.id, indexed |
| username | String(50) | no |  |
| action | String(40) | no | indexed |
| entity_type | String(30) | no |  |
| entity_id | Integer | yes |  |
| entity_label | String(200) | yes |  |
| summary | Text | yes |  |
| old_values | Text | yes |  |
| new_values | Text | yes |  |
| workstation | String(100) | yes |  |
| id | Integer | no | PK |

* INDEX ix_audit_log_entity (entity_type, entity_id)

## bins

| Column | Type | Null | Key / reference |
|---|---|---|---|
| location_id | Integer | no | FK locations.id, indexed |
| code | String(30) | no |  |
| description | String(200) | yes |  |
| is_available | Boolean | no |  |
| full_code | String(450) | no | UNIQUE |
| notes | Text | yes |  |
| barcode | String(80) | yes |  |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| is_active | Boolean | no |  |

* UNIQUE (location_id, code)
* UNIQUE INDEX uq_bins_barcode (barcode)

## bom_items

| Column | Type | Null | Key / reference |
|---|---|---|---|
| parent_product_id | Integer | no | FK products.id, indexed |
| component_product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| notes | Text | yes |  |
| id | Integer | no | PK |

## categories

| Column | Type | Null | Key / reference |
|---|---|---|---|
| name | String(100) | no |  |
| parent_id | Integer | yes | FK categories.id, indexed |
| description | Text | yes |  |
| id | Integer | no | PK |
| is_active | Boolean | no |  |

* UNIQUE (parent_id, name)

## delivery_discrepancies

| Column | Type | Null | Key / reference |
|---|---|---|---|
| number | String(30) | no | UNIQUE |
| supplier_id | Integer | no | FK suppliers.id, indexed |
| supplier_name_snapshot | String(200) | yes |  |
| purchase_order_id | Integer | yes | FK purchase_orders.id, indexed |
| purchase_order_line_id | Integer | yes | FK purchase_order_lines.id, indexed |
| delivery_note_id | Integer | yes | FK delivery_notes.id, indexed |
| product_id | Integer | yes | FK products.id, indexed |
| reason_code | String(30) | no | FK reason_codes.code, indexed |
| expected_qty | Quantity | yes |  |
| received_qty | Quantity | yes |  |
| quantity | Quantity | no |  |
| notes | Text | yes |  |
| is_automatic | Boolean | no |  |
| status | String(20) | no | indexed |
| resolution | Text | yes |  |
| resolved_at | UTCDateTime | yes |  |
| resolved_by_id | Integer | yes | FK users.id, indexed |
| created_by_id | Integer | no | FK users.id, indexed |
| created_at | UTCDateTime | no |  |
| id | Integer | no | PK |
| client_token | String(40) | yes | UNIQUE, indexed |

* CHECK quantity >= 0
* CHECK status IN ('OPEN', 'RESOLVED')
* UNIQUE INDEX ix_delivery_discrepancies_client_token (client_token)

## delivery_note_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| delivery_note_id | Integer | no | FK delivery_notes.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| unit_cost | Money | yes |  |
| bin_id | Integer | no | FK bins.id, indexed |
| notes | Text | yes |  |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| purchase_order_line_id | Integer | yes | FK purchase_order_lines.id, indexed |
| id | Integer | no | PK |

## delivery_notes

| Column | Type | Null | Key / reference |
|---|---|---|---|
| dn_number | String(80) | no | indexed |
| dn_number_norm | String(80) | no |  |
| supplier_id | Integer | no | FK suppliers.id, indexed |
| delivery_date | Date | no | indexed |
| po_reference | String(80) | yes | indexed |
| received_by_id | Integer | no | FK users.id, indexed |
| reversed_by_id | Integer | yes | FK users.id, indexed |
| supplier_name_snapshot | String(200) | yes |  |
| purchase_order_id | Integer | yes | FK purchase_orders.id, indexed |
| id | Integer | no | PK |
| status | String(20) | no | indexed |
| notes | Text | yes |  |
| created_at | UTCDateTime | no |  |
| reversed_at | UTCDateTime | yes |  |
| reversal_reason | Text | yes |  |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_delivery_notes_client_token (client_token)
* UNIQUE INDEX uq_delivery_notes_supplier_number (supplier_id, dn_number_norm) WHERE status = 'POSTED'

## finished_product_info

| Column | Type | Null | Key / reference |
|---|---|---|---|
| product_id | Integer | no | PK, FK products.id |
| production_date | Date | yes |  |
| site_id | Integer | yes | FK sites.id, indexed |
| customer | String(200) | yes |  |
| status | String(20) | no |  |

## goods_issue_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| issue_id | Integer | no | FK goods_issues.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| unit_cost | Money | yes |  |
| bin_id | Integer | no | FK bins.id, indexed |
| notes | Text | yes |  |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| id | Integer | no | PK |

## goods_issues

| Column | Type | Null | Key / reference |
|---|---|---|---|
| issue_number | String(30) | no | UNIQUE |
| issue_date | Date | no | indexed |
| site_id | Integer | no | FK sites.id, indexed |
| requested_by | String(120) | yes |  |
| issued_by | String(120) | no |  |
| reference | String(80) | yes | indexed |
| user_id | Integer | no | FK users.id, indexed |
| reversed_by_id | Integer | yes | FK users.id, indexed |
| id | Integer | no | PK |
| site_number_snapshot | String(50) | yes |  |
| site_name_snapshot | String(200) | yes |  |
| status | String(20) | no | indexed |
| notes | Text | yes |  |
| created_at | UTCDateTime | no |  |
| reversed_at | UTCDateTime | yes |  |
| reversal_reason | Text | yes |  |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_goods_issues_client_token (client_token)

## locations

| Column | Type | Null | Key / reference |
|---|---|---|---|
| parent_id | Integer | yes | FK locations.id, indexed |
| kind | String(20) | no |  |
| code | String(30) | no |  |
| name | String(120) | no |  |
| notes | Text | yes |  |
| path | String(400) | no | indexed |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| is_active | Boolean | no |  |

* UNIQUE (parent_id, code)

## movement_reversals

| Column | Type | Null | Key / reference |
|---|---|---|---|
| original_movement_id | Integer | no | FK stock_movements.id |
| reversal_movement_id | Integer | no | FK stock_movements.id |
| reason_code | String(30) | no | FK reason_codes.code, indexed |
| reason | Text | no |  |
| reversed_by_id | Integer | no | FK users.id, indexed |
| reversed_by_name_snapshot | String(120) | yes |  |
| reversed_at | UTCDateTime | no |  |
| id | Integer | no | PK |

* CHECK original_movement_id <> reversal_movement_id
* UNIQUE (original_movement_id)
* UNIQUE (reversal_movement_id)

## number_sequences

| Column | Type | Null | Key / reference |
|---|---|---|---|
| name | String(30) | no | PK |
| prefix | String(10) | no |  |
| next_value | Integer | no |  |
| padding | Integer | no |  |

## production_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| order_id | Integer | no | FK production_orders.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| bin_id | Integer | no | FK bins.id, indexed |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| id | Integer | no | PK |

## production_orders

| Column | Type | Null | Key / reference |
|---|---|---|---|
| order_number | String(30) | no | UNIQUE |
| finished_product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| output_bin_id | Integer | no | FK bins.id, indexed |
| site_id | Integer | yes | FK sites.id, indexed |
| production_date | Date | no | indexed |
| user_id | Integer | no | FK users.id, indexed |
| output_movement_id | Integer | yes | FK stock_movements.id, indexed |
| reversed_by_id | Integer | yes | FK users.id, indexed |
| id | Integer | no | PK |
| site_number_snapshot | String(50) | yes |  |
| site_name_snapshot | String(200) | yes |  |
| status | String(20) | no | indexed |
| notes | Text | yes |  |
| created_at | UTCDateTime | no |  |
| reversed_at | UTCDateTime | yes |  |
| reversal_reason | Text | yes |  |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_production_orders_client_token (client_token)

## products

| Column | Type | Null | Key / reference |
|---|---|---|---|
| item_type | String(20) | no | indexed |
| internal_code | String(50) | no | UNIQUE |
| supplier_code | String(80) | yes | indexed |
| barcode | String(80) | yes | indexed |
| name | String(200) | no | indexed |
| description | Text | yes |  |
| brand | String(100) | yes |  |
| category_id | Integer | yes | FK categories.id, indexed |
| unit_id | Integer | no | FK units.id, indexed |
| supplier_id | Integer | yes | FK suppliers.id, indexed |
| default_bin_id | Integer | yes | FK bins.id, indexed |
| min_stock | Quantity | yes |  |
| reorder_qty | Quantity | yes |  |
| unit_cost | Money | yes |  |
| notes | Text | yes |  |
| created_by_id | Integer | yes | FK users.id, indexed |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| is_active | Boolean | no |  |
| version | Integer | no |  |

## purchase_order_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| purchase_order_id | Integer | no | FK purchase_orders.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| unit_cost | Money | yes |  |
| notes | Text | yes |  |
| id | Integer | no | PK |

* CHECK quantity > 0
* UNIQUE (purchase_order_id, line_no)

## purchase_orders

| Column | Type | Null | Key / reference |
|---|---|---|---|
| po_number | String(30) | no | UNIQUE |
| supplier_id | Integer | no | FK suppliers.id, indexed |
| supplier_name_snapshot | String(200) | yes |  |
| site_id | Integer | yes | FK sites.id, indexed |
| order_date | Date | no | indexed |
| expected_date | Date | yes | indexed |
| supplier_reference | String(80) | yes |  |
| status | String(20) | no | indexed |
| notes | Text | yes |  |
| created_by_id | Integer | no | FK users.id, indexed |
| created_at | UTCDateTime | no |  |
| ordered_at | UTCDateTime | yes |  |
| closed_at | UTCDateTime | yes |  |
| closed_by_id | Integer | yes | FK users.id, indexed |
| close_reason | Text | yes |  |
| id | Integer | no | PK |
| site_number_snapshot | String(50) | yes |  |
| site_name_snapshot | String(200) | yes |  |
| client_token | String(40) | yes | UNIQUE, indexed |

* CHECK status IN ('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED')
* UNIQUE INDEX ix_purchase_orders_client_token (client_token)

## reason_codes

| Column | Type | Null | Key / reference |
|---|---|---|---|
| code | String(30) | no | UNIQUE |
| label | String(100) | no |  |
| for_adjustment | Boolean | no |  |
| for_reversal | Boolean | no |  |
| for_return | Boolean | no |  |
| for_discrepancy | Boolean | no |  |
| is_system | Boolean | no |  |
| sort_order | Integer | no |  |
| id | Integer | no | PK |
| is_active | Boolean | no |  |

## recent_searches

| Column | Type | Null | Key / reference |
|---|---|---|---|
| user_id | Integer | no | FK users.id, indexed |
| text | String(120) | no |  |
| searched_at | UTCDateTime | no |  |
| id | Integer | no | PK |

* UNIQUE (user_id, text)

## return_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| return_id | Integer | no | FK stock_returns.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| bin_id | Integer | no | FK bins.id, indexed |
| condition | String(20) | no |  |
| notes | Text | yes |  |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| id | Integer | no | PK |

## role_permissions

| Column | Type | Null | Key / reference |
|---|---|---|---|
| role_id | Integer | no | PK, FK roles.id |
| permission | String(60) | no | PK |

## roles

| Column | Type | Null | Key / reference |
|---|---|---|---|
| code | String(30) | no | UNIQUE |
| name | String(80) | no |  |
| description | Text | yes |  |
| is_system | Boolean | no |  |
| id | Integer | no | PK |

## saved_searches

| Column | Type | Null | Key / reference |
|---|---|---|---|
| user_id | Integer | no | FK users.id, indexed |
| name | String(80) | no |  |
| criteria | Text | no |  |
| created_at | UTCDateTime | no |  |
| id | Integer | no | PK |

* UNIQUE (user_id, name)

## settings

| Column | Type | Null | Key / reference |
|---|---|---|---|
| key | String(60) | no | PK |
| value | Text | yes |  |

## sites

| Column | Type | Null | Key / reference |
|---|---|---|---|
| project_number | String(50) | no | UNIQUE |
| name | String(200) | no | indexed |
| customer | String(200) | yes | indexed |
| address | Text | yes |  |
| postcode | String(12) | yes |  |
| project_manager | String(120) | yes |  |
| contact_name | String(120) | yes |  |
| contact_phone | String(50) | yes |  |
| contact_email | String(200) | yes |  |
| status | String(20) | no | indexed |
| start_date | Date | yes |  |
| end_date | Date | yes |  |
| notes | Text | yes |  |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| version | Integer | no |  |

## stock_adjustments

| Column | Type | Null | Key / reference |
|---|---|---|---|
| adjustment_number | String(30) | no | UNIQUE |
| product_id | Integer | no | FK products.id, indexed |
| bin_id | Integer | no | FK bins.id, indexed |
| previous_qty | Quantity | no |  |
| new_qty | Quantity | no |  |
| difference | Quantity | no |  |
| reason_code | String(30) | no |  |
| reason | Text | no |  |
| user_id | Integer | no | FK users.id, indexed |
| created_at | UTCDateTime | no | indexed |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| id | Integer | no | PK |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_stock_adjustments_client_token (client_token)

## stock_count_lines

| Column | Type | Null | Key / reference |
|---|---|---|---|
| count_id | Integer | no | FK stock_counts.id, indexed |
| line_no | Integer | no |  |
| product_id | Integer | no | FK products.id, indexed |
| bin_id | Integer | no | FK bins.id, indexed |
| system_qty | Quantity | no |  |
| counted_qty | Quantity | yes |  |
| counted_by_id | Integer | yes | FK users.id, indexed |
| counted_at | UTCDateTime | yes |  |
| notes | Text | yes |  |
| adjustment_id | Integer | yes | FK stock_adjustments.id, indexed |
| id | Integer | no | PK |

* UNIQUE (count_id, product_id, bin_id)

## stock_counts

| Column | Type | Null | Key / reference |
|---|---|---|---|
| count_number | String(30) | no | UNIQUE |
| status | String(20) | no | indexed |
| location_id | Integer | yes | FK locations.id, indexed |
| description | String(200) | yes |  |
| notes | Text | yes |  |
| created_by_id | Integer | no | FK users.id, indexed |
| created_at | UTCDateTime | no | indexed |
| submitted_by_id | Integer | yes | FK users.id, indexed |
| submitted_at | UTCDateTime | yes |  |
| closed_by_id | Integer | yes | FK users.id, indexed |
| closed_at | UTCDateTime | yes |  |
| close_reason | Text | yes |  |
| id | Integer | no | PK |

## stock_levels

| Column | Type | Null | Key / reference |
|---|---|---|---|
| product_id | Integer | no | FK products.id, indexed |
| bin_id | Integer | no | FK bins.id, indexed |
| quantity | Quantity | no |  |
| updated_at | UTCDateTime | no |  |
| id | Integer | no | PK |
| version | Integer | no |  |

* UNIQUE (product_id, bin_id)

## stock_movements

| Column | Type | Null | Key / reference |
|---|---|---|---|
| movement_type | String(30) | no | indexed |
| product_id | Integer | no | FK products.id, indexed |
| quantity | Quantity | no |  |
| from_bin_id | Integer | yes | FK bins.id, indexed |
| to_bin_id | Integer | yes | FK bins.id, indexed |
| unit_cost | Money | yes |  |
| site_id | Integer | yes | FK sites.id, indexed |
| supplier_id | Integer | yes | FK suppliers.id, indexed |
| doc_type | String(30) | yes |  |
| doc_id | Integer | yes |  |
| doc_number | String(80) | yes | indexed |
| reference | String(120) | yes | indexed |
| notes | Text | yes |  |
| user_id | Integer | no | FK users.id, indexed |
| occurred_at | UTCDateTime | no | indexed |
| recorded_at | UTCDateTime | no |  |
| reversal_of_id | Integer | yes | FK stock_movements.id, indexed |
| is_override | Boolean | no |  |
| override_reason | Text | yes |  |
| product_code_snapshot | String(50) | yes |  |
| product_name_snapshot | String(200) | yes |  |
| from_bin_name_snapshot | String(450) | yes |  |
| to_bin_name_snapshot | String(450) | yes |  |
| site_number_snapshot | String(50) | yes |  |
| site_name_snapshot | String(200) | yes |  |
| supplier_name_snapshot | String(200) | yes |  |
| user_name_snapshot | String(120) | yes |  |
| id | Integer | no | PK |

* CHECK from_bin_id IS NOT NULL OR to_bin_id IS NOT NULL
* CHECK quantity > 0
* INDEX ix_stock_movements_doc (doc_type, doc_id)
* INDEX ix_stock_movements_product_time (product_id, occurred_at)
* UNIQUE INDEX uq_stock_movements_reversal_of (reversal_of_id) WHERE reversal_of_id IS NOT NULL

## stock_returns

| Column | Type | Null | Key / reference |
|---|---|---|---|
| return_number | String(30) | no | UNIQUE |
| return_type | String(20) | no | indexed |
| return_date | Date | no | indexed |
| site_id | Integer | yes | FK sites.id, indexed |
| supplier_id | Integer | yes | FK suppliers.id, indexed |
| reference | String(80) | yes | indexed |
| reason | Text | yes |  |
| reason_code | String(30) | yes |  |
| user_id | Integer | no | FK users.id, indexed |
| reversed_by_id | Integer | yes | FK users.id, indexed |
| supplier_name_snapshot | String(200) | yes |  |
| id | Integer | no | PK |
| site_number_snapshot | String(50) | yes |  |
| site_name_snapshot | String(200) | yes |  |
| status | String(20) | no | indexed |
| notes | Text | yes |  |
| created_at | UTCDateTime | no |  |
| reversed_at | UTCDateTime | yes |  |
| reversal_reason | Text | yes |  |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_stock_returns_client_token (client_token)

## stock_transfers

| Column | Type | Null | Key / reference |
|---|---|---|---|
| transfer_number | String(30) | no | UNIQUE |
| product_id | Integer | no | FK products.id, indexed |
| from_bin_id | Integer | no | FK bins.id, indexed |
| to_bin_id | Integer | no | FK bins.id, indexed |
| quantity | Quantity | no |  |
| notes | Text | yes |  |
| user_id | Integer | no | FK users.id, indexed |
| created_at | UTCDateTime | no | indexed |
| movement_id | Integer | yes | FK stock_movements.id, indexed |
| id | Integer | no | PK |
| client_token | String(40) | yes | UNIQUE, indexed |

* UNIQUE INDEX ix_stock_transfers_client_token (client_token)

## suppliers

| Column | Type | Null | Key / reference |
|---|---|---|---|
| code | String(30) | no | UNIQUE |
| name | String(200) | no | indexed |
| contact_name | String(120) | yes |  |
| phone | String(50) | yes |  |
| email | String(200) | yes |  |
| address | Text | yes |  |
| account_number | String(50) | yes |  |
| notes | Text | yes |  |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| is_active | Boolean | no |  |

## units

| Column | Type | Null | Key / reference |
|---|---|---|---|
| code | String(12) | no | UNIQUE |
| name | String(50) | no |  |
| allow_decimal | Boolean | no |  |
| id | Integer | no | PK |
| is_active | Boolean | no |  |

## users

| Column | Type | Null | Key / reference |
|---|---|---|---|
| username | String(50) | no | UNIQUE |
| full_name | String(120) | no |  |
| email | String(200) | yes |  |
| password_hash | String(100) | no |  |
| role_id | Integer | no | FK roles.id, indexed |
| must_change_password | Boolean | no |  |
| last_login_at | UTCDateTime | yes |  |
| id | Integer | no | PK |
| created_at | UTCDateTime | no |  |
| updated_at | UTCDateTime | no |  |
| is_active | Boolean | no |  |
