import datetime
import os
import logging
import pandas as pd
import numpy as np
from sqlalchemy import desc
from flask_sqlalchemy import SQLAlchemy
from anyway.parsers import infographics_data_cache_updater
from anyway.parsers import timezones
from anyway.models import NewsFlash
from anyway.slack_accident_notifications import publish_notification
from anyway.utilities import trigger_airflow_dag
from anyway.widgets.widget_utils import newsflash_has_location

# fmt: off


def init_db() -> "DBAdapter":
    from anyway.app_and_db import db
    return DBAdapter(db)


class DBAdapter:
    def __init__(self, db: SQLAlchemy):
        self.db = db
        self.__null_types: set = {np.nan}

    def execute(self, *args, **kwargs):
        return self.db.session.execute(*args, **kwargs)

    def commit(self, *args, **kwargs):
        return self.db.session.commit(*args, **kwargs)

    def get_markers_for_location_extraction(self):
        query_res = self.execute(
            """SELECT * FROM cbs_locations"""
        )
        df = pd.DataFrame(query_res.fetchall())
        df.columns = query_res.keys()
        return df

    def remove_duplicate_rows(self):
        """
        remove duplicate rows by link
        """
        self.execute(
            """
            DELETE FROM news_flash T1
            USING news_flash T2
            WHERE T1.ctid < T2.ctid  -- delete the older versions
            AND T1.link  = T2.link;  -- add more columns if needed
            """
        )
        self.commit()

    @staticmethod
    def generate_infographics_and_send_to_telegram(newsflashid):
        dag_conf = {"news_flash_id": newsflashid}
        trigger_airflow_dag("generate-and-send-infographics-images", dag_conf)

    @staticmethod
    def publish_notifications(newsflash: NewsFlash):
        publish_notification(newsflash)
        if newsflash_has_location(newsflash):
            DBAdapter.generate_infographics_and_send_to_telegram(newsflash.id)
        else:
            logging.debug("newsflash does not have location, not publishing")

    def insert_new_newsflash(self, newsflash: NewsFlash) -> None:
        logging.info("Adding newsflash, is accident: {}, date: {}"
                     .format(newsflash.accident, newsflash.date))
        self.__fill_na(newsflash)
        self.db.session.add(newsflash)
        self.db.session.commit()
        infographics_data_cache_updater.add_news_flash_to_cache(newsflash)
        if os.environ.get("FLASK_ENV") == "production" and newsflash.accident:
            try:
                DBAdapter.publish_notifications(newsflash)
            except Exception as e:
                logging.error("publish notifications failed")
                logging.error(e)


    def get_newsflash_by_id(self, id):
        return self.db.session.query(NewsFlash).filter(NewsFlash.id == id)

    def select_newsflash_where_source(self, source):
        return self.db.session.query(NewsFlash).filter(NewsFlash.source == source)

    def get_all_newsflash(self):
        return self.db.session.query(NewsFlash).order_by(desc(NewsFlash.date))

    def get_latest_date_of_source(self, source):
        """
        :return: latest date of news flash
        """
        latest_date = self.execute(
            "SELECT max(date) FROM news_flash WHERE source=:source",
            {"source": source},
        ).fetchone()[0] or datetime.datetime(1900, 1, 1, 0, 0, 0)
        res = timezones.from_db(latest_date)
        logging.info('Latest time fetched for source {} is {}'
                     .format(source, res))
        return res

    def get_latest_tweet_id(self):
        """
        :return: latest tweet id
        """
        latest_id = self.execute(
            "SELECT tweet_id FROM news_flash where source='twitter' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if latest_id:
            return latest_id[0]
        return None

    def __fill_na(self, newsflash: NewsFlash):
        for key, value in newsflash.__dict__.items():
            if value in self.__null_types:
                setattr(newsflash, key, None)
