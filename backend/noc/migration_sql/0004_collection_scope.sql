CREATE FUNCTION enforce_monitoring_scope() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.site <> '_all' AND NOT EXISTS (SELECT 1 FROM sites WHERE name=NEW.site AND selected) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER metrics_scope BEFORE INSERT ON metrics FOR EACH ROW EXECUTE FUNCTION enforce_monitoring_scope();
CREATE TRIGGER devices_scope BEFORE INSERT OR UPDATE ON devices FOR EACH ROW EXECUTE FUNCTION enforce_monitoring_scope();
CREATE TRIGGER clients_scope BEFORE INSERT OR UPDATE ON clients FOR EACH ROW EXECUTE FUNCTION enforce_monitoring_scope();
CREATE TRIGGER events_scope BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION enforce_monitoring_scope();
CREATE TRIGGER alerts_scope BEFORE INSERT ON alerts FOR EACH ROW EXECUTE FUNCTION enforce_monitoring_scope();