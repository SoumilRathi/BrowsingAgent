import os
import time
import json
from browserbase import Browserbase
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from agents.content_agent import ContentAgent
from utils.use_supabase import insert_tbl
from utils.use_claude import use_claude

load_dotenv()

BROWSERBASE_API_KEY = os.environ["BROWSERBASE_API_KEY"]
BROWSERBASE_PROJECT_ID = os.environ["BROWSERBASE_PROJECT_ID"]
TWITTER_USERNAME = os.environ["TWITTER_USERNAME"]
TWITTER_PASSWORD = os.environ["TWITTER_PASSWORD"]
TWITTER_EMAIL = os.environ["TWITTER_EMAIL"]

bb = Browserbase(api_key=BROWSERBASE_API_KEY)


class BrowserAgent:
    def __init__(self, audience_persona: str, tweet_style: str):
        self.playwright = None
        self.browser = None
        self.page = None
        self.audience_persona = audience_persona
        self.tweet_style = tweet_style
        self.history = {}  # Dictionary mapping search terms to list of result strings
        self.current_search_term = None
        self.content_agent = ContentAgent(audience_persona=self.audience_persona, tweet_style=self.tweet_style)

   
    def initialize_browser(self):
        """Initialize browser and login to Twitter"""
        print("Initializing Twitter")

        # Create a session on Browserbase
        session = bb.sessions.create(project_id=BROWSERBASE_PROJECT_ID)
        print("Session replay URL:", f"https://browserbase.com/sessions/{session.id}")

        debug_urls = bb.sessions.debug(session.id)

        debug_connection_url = debug_urls.debugger_fullscreen_url

        print(f"Debug connection URL: {debug_connection_url}")
        insert_tbl("research_sessions", {"session_id": session.id, "debug_url": debug_connection_url})

        # Connect to the remote session
        chromium = self.playwright.chromium
        self.browser = chromium.connect_over_cdp(session.connect_url)
        context = self.browser.contexts[0]
        self.page = context.pages[0]

        # Login process
        self.page.goto("https://twitter.com/i/flow/login")
        time.sleep(2)

        self.page.get_by_label("Phone, email, or username").fill(TWITTER_USERNAME)
        self.page.get_by_role("button", name="Next").click()
        time.sleep(1)

        try:
            email_field = self.page.get_by_label("Phone or email", exact=True)
            if email_field.is_visible():
                email_field.fill(TWITTER_EMAIL)
                self.page.get_by_role("button", name="Next").click()
                time.sleep(1)
        except:
            pass

        self.page.get_by_label("Password", exact=True).fill(TWITTER_PASSWORD)
        self.page.get_by_role("button", name="Log in").click()
        time.sleep(3)

    def extract_tweet_data(self, tweet):
        """Extract data from a tweet to enable frontend replication

        Returns a dictionary containing all essential tweet components:
        - tweet_text: The main content of the tweet
        - author: Object containing name and username
        - avatar_url: URL of the user's profile image
        - timestamp: When the tweet was posted
        - metrics: Engagement metrics (likes, replies, etc)
        - media: Any attached images/videos
        """

        print(f"Extracting tweet data from {tweet}")
        # Core tweet content
        tweet_text = tweet.locator('[data-testid="tweetText"]').first.inner_text()

        # Author details
        author_element = tweet.locator('[data-testid="User-Name"]')
        author = author_element.locator('span').first.inner_text()

        print(f"Author: {author}")

        # Avatar URL
        avatar_url = tweet.locator('[data-testid="Tweet-User-Avatar"] img').first.get_attribute(
            'src'
        )


        print(f"Avatar URL: {avatar_url}")
        # Timestamp and URL (they're in the same link element)
        time_link = tweet.locator('time').locator('xpath=..').first
        timestamp = time_link.locator('time').first.get_attribute('datetime')
        relative_time = time_link.locator('time').first.inner_text()
        tweet_url = time_link.get_attribute('href')
        if tweet_url and not tweet_url.startswith('http'):
            tweet_url = f'https://twitter.com{tweet_url}'

        # Media attachments
        # print(f"Extracting media from {tweet}")
        media = False
        media_elements = tweet.locator(
            '[data-testid="tweetPhoto"], [data-testid="tweetVideo"]'
        ).all()
        for element in media_elements:
            if element.get_attribute('data-testid') == 'tweetPhoto':
                media = True
            else:
                media = True
        # Get engagement metrics
        metrics = self.get_metrics(tweet)
        
        return {
            'tweet_text': tweet_text,
            'author': author,
            'avatar_url': avatar_url,
            'timestamp': timestamp,
            'relative_time': relative_time,
            'tweet_url': tweet_url,
            'metrics': metrics,
            'media': media,
        }

    def get_metrics(self, tweet) -> dict:
        """
        Evaluate a tweet by extracting engagement metrics
        Returns a dictionary of metrics
        """
        try:
            # Get engagement metrics
            metrics = {'likes': 0, 'views': 0, 'replies': 0, 'reposts': 0}

            # Find metrics using specific data-testid attributes
            metrics_elements = {
                'replies': tweet.locator('[data-testid="reply"]').first,
                'reposts': tweet.locator('[data-testid="retweet"]').first,
                'likes': tweet.locator('[data-testid="like"]').first,
                'views': tweet.locator('a[href*="/analytics"]').first,
            }

            for metric_type, element in metrics_elements.items():
                try:
                    # Get the text content and extract just the number
                    text = element.inner_text()
                    if text:
                        # Handle K (thousands) and M (millions) suffixes
                        if 'K' in text:
                            value = float(text.replace('K', '')) * 1000
                        elif 'M' in text:
                            value = float(text.replace('M', '')) * 1000000
                        else:
                            value = float(text.replace(',', ''))
                        metrics[metric_type] = int(value)
                except Exception:
                    continue

            return metrics

        except Exception as e:
            print(f"Error evaluating tweet: {str(e)}")
            return None

    def should_engage(self, tweet_data: dict) -> bool:
        metrics = tweet_data['metrics']
        total_views = metrics['views'] or 1

        # Calculate engagement metrics
        replies = metrics['replies'] or 0
        reposts = metrics['reposts'] or 0
        likes = metrics['likes'] or 0

        engagement_rate = ((replies + reposts + likes) / total_views) * 100
        reply_rate = (replies / total_views) * 100

        # Analyze text content quality
        tweet_text = tweet_data['tweet_text'].lower()

        # Content quality indicators
        has_question = any(char in tweet_text for char in '?¿')
        has_insight_markers = any(
            phrase in tweet_text
            for phrase in [
                'here\'s why',
                'the key is',
                'i learned',
                'thread',
                'tip:',
                'guide:',
                'how to',
                'mistake:',
                'lesson:',
            ]
        )
        has_engagement_hooks = any(
            phrase in tweet_text
            for phrase in [
                'what do you think',
                'agree?',
                'your thoughts',
                'what\'s your',
                'who else',
                'reply if',
            ]
        )

        # Scoring system focused on text quality
        score = 0

        # Engagement quality (weighted more heavily for text-only)
        if reply_rate > 0.15:
            score += 3  # Higher threshold for text-only
        if engagement_rate > 2.5:
            score += 2  # Higher threshold for text-only
        if replies > 8:
            score += 2  # Higher minimum replies for text

        # Content quality (more important for text-only tweets)
        if has_question:
            score += 2
        if has_insight_markers:
            score += 3  # Value-giving content
        if has_engagement_hooks:
            score += 2

        # Red flags specific to text content
        if len(tweet_text) < 50:
            score -= 1  # Too short might lack substance
        if total_views > 10000 and replies < 5:
            score -= 2  # Poor discussion rate
        if engagement_rate < 0.2:
            score -= 2  # Higher minimum for text-only

        print(f"Score: {score}")
        return score >= 2  # Higher threshold for text-only tweets

    def generate_content(self, tweet_data: dict):
        """Generate a reply to a tweet"""
        self.content_agent.generate_reply(tweet_data)


    def search_twitter(self, search_term):
        """Browse Twitter for a specific search term and evaluate tweets"""

        self.current_search_term = search_term
        try:
            if search_term not in self.history:
                self.history[search_term] = []

            # Navigate to search
            search_url = (
                f"https://twitter.com/search?q={search_term}&src=typed_query&f=top"
            )
            self.page.goto(search_url)
            time.sleep(1)

            processed_tweets = set()  # Keep track of processed tweets
            tweets_analyzed = 0

            while tweets_analyzed < 25:
                # Get all visible tweets
                tweets = self.page.locator('article[data-testid="tweet"]').all()

                # Process new tweets
                for tweet in tweets:
                    tweet_data = self.extract_tweet_data(tweet)
                    # Get a unique identifier for the tweet (using the text content)
                    tweet_text = tweet_data['tweet_text']
                    self.history[search_term].append(tweet_text)

                    should_engage = self.should_engage(tweet_data)
                    if (should_engage and not (tweet_text in processed_tweets)):
                        processed_tweets.add(tweet_text)
                        tweets_analyzed += 1
                        print("Generating content for this tweet")
                        self.generate_content(tweet_data)
                    else:
                        print("This tweet is not good enough to engage with")
                    if tweets_analyzed >= 25:
                        break

                # Scroll down to load more tweets
                self.page.evaluate("window.scrollBy(0, 1000)")
                time.sleep(0.5)

        except Exception as e:
            print(f"An error occurred while browsing: {str(e)}")
        finally:
            # Close the browser
            self.page.close()
            self.browser.close()
            print("\nDone!")

    def continue_search(self):
        """Continue a search for a specific search term"""
        processed_tweets = set()  # Keep track of processed tweets
        tweets_analyzed = 0

        while tweets_analyzed < 25:
            # Get all visible tweets
            tweets = self.page.locator('article[data-testid="tweet"]').all()

            # Process new tweets
            for tweet in tweets:
                tweet_data = self.extract_tweet_data(tweet)
                # Get a unique identifier for the tweet (using the text content)
                tweet_text = tweet_data['tweet_text']
                self.history[self.current_search_term].append(tweet_text)

                should_engage = self.should_engage(tweet_data)
                if (should_engage and not tweet_text in processed_tweets):
                    processed_tweets.add(tweet_text)
                    tweets_analyzed += 1
                    print("Generating content for this tweet")
                    self.generate_content(tweet_data)
                else:
                    print("This tweet is not good enough to engage with")
                if tweets_analyzed >= 25:
                    break

            # Scroll down to load more tweets
            self.page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(0.5)


    def make_decision(self, search_term):
        """Calling anthropic to make a decision abt what to do rn"""
        prompt = (
            f"""
        You are an agent capable of browsing twitter. You have been given a search term that the user wants to find good tweets to reply to.

        You have been given the searches that you have made so far, and should use that information and quality of tweets found to make your twitter searches better now.

        Your goal is to find around 20 good tweets to reply to.

        Based on this information, you have to either choose to make a new search or to stop the browsing entirely if you think you have enough good tweets.

        # Original search term
        {search_term}

        # Searches so far
        """
            + "\n".join(
                [
                    f"## {search_term}\n\nTweets:\n"
                    + "\n".join([f"{i+1}. {tweet}" for i, tweet in enumerate(tweets)])
                    for search_term, tweets in self.history.items()
                ]
            )
            + """

        # Actions
        1. search "search term" - this makes a new search for a new search term
        2. continue - this continues the current search term and scrolls down to load more tweets
        3. stop - this stops the browsing process entirely

        You should output your final action decision as follows:
        <final>
        [action]
        </final>
        """
        )

        response = use_claude(prompt)

        # Extract action from response by finding text between <final> and </final> tags
        action = response.split("<final>")[1].split("</final>")[0].strip()
        return action

    def execute_decision(self, decision):
        """Execute the decision"""
        if decision.startswith("search"):
            # Extract search term from the decision string
            search_term = decision[7:].strip().strip('"')
            self.current_search_term = decision[7:].strip().strip('"')
            print(f"Searching for: {search_term}")
            self.search_twitter(search_term)
            return True
        elif decision == "continue":
            print(f"Continuing search for: {self.current_search_term}")
            self.continue_search()
            return True
        elif decision == "stop":
            print("Stopping browsing")
            self.page.close()
            self.browser.close()
            return False
        return False

    def browse_twitter(self, search_term):
        """This function is the entire decision loop for the browsing agent"""

        with sync_playwright() as p:
            self.playwright = p
            self.initialize_browser()
            start_time = time.time()

            print("Starting browsing")
            while True:
                # Check if 5 minutes have elapsed
                if time.time() - start_time > 300:  # 300 seconds = 5 minutes
                    print("Reached 5 minute time limit")
                    break

                # Get next decision from the agent
                decision = self.make_decision(search_term)

                print(f"Decision: {decision}")

                # Execute the decision and check if we should continue
                if not self.execute_decision(decision):
                    break


if __name__ == "__main__":
    browser_agent = BrowserAgent(
        audience_persona="49ers fans who like to shit on other teams",
        tweet_style="Blunt, rude, funny, making fun of everything except the 49ers",
    )
    browser_agent.browse_twitter("American Football")
